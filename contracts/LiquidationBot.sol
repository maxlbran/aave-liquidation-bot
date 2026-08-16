// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

// ─────────────────────────────────────────────
//  Interfaces
// ─────────────────────────────────────────────

interface IPool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;

    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;
}

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function decimals() external view returns (uint8);
}

interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external returns (uint256 amountOut);
    function exactInput(ExactInputParams calldata params) external returns (uint256 amountOut);
}

// ─────────────────────────────────────────────
//  Main Contract
// ─────────────────────────────────────────────

/**
 * @title  LiquidationBot
 * @notice Flash-loan powered Aave V3 liquidation bot.
 *
 * Flow (all atomic in one tx):
 *   1. executeLiquidation() -> Aave flashLoanSimple()
 *   2. Aave calls back executeOperation() with loan funds
 *   3. We call Aave liquidationCall()  → receive collateral + bonus
 *   4. Swap collateral → debtAsset via Uniswap V3
 *   5. Repay flash loan (principal + 0.05% premium)
 *   6. Assert remaining balance ≥ minProfit
 *
 * Profits accumulate in the contract; owner withdraws at will.
 */
contract LiquidationBot {

    // ── State ──────────────────────────────────────────────────────────────
    address public owner;
    IPool   public immutable POOL;
    ISwapRouter public immutable SWAP_ROUTER;

    // ── Events ─────────────────────────────────────────────────────────────
    event LiquidationExecuted(
        address indexed liquidatedUser,
        address indexed collateralAsset,
        address indexed debtAsset,
        uint256 debtCovered,
        uint256 collateralReceived,
        uint256 profit           // denominated in debtAsset
    );

    event Withdrawn(address indexed token, uint256 amount, address indexed to);

    // ── Errors ─────────────────────────────────────────────────────────────
    error OnlyOwner();
    error OnlyPool();
    error OnlySelf();
    error NoCollateralReceived();
    error InsufficientProfit(uint256 actual, uint256 required);
    error ZeroAddress();

    // ── Packed call-data passed through flash loan ──────────────────────────
    struct LiqParams {
        address collateralAsset;
        address debtAsset;
        address userToLiquidate;
        uint256 debtToCover;        // exact amount to liquidate (≤ 50% of debt)
        uint24  swapFee;            // Uniswap V3 fee tier (500 / 3000 / 10000)
        bytes   swapPath;           // abi-encoded multi-hop path (empty = single-hop)
        uint256 minProfitWei;       // min profit in debtAsset units (revert if below)
    }

    // ── Constructor ────────────────────────────────────────────────────────
    constructor(address _pool, address _swapRouter) {
        if (_pool == address(0) || _swapRouter == address(0)) revert ZeroAddress();
        owner       = msg.sender;
        POOL        = IPool(_pool);
        SWAP_ROUTER = ISwapRouter(_swapRouter);
    }

    // ── Modifiers ──────────────────────────────────────────────────────────
    modifier onlyOwner() {
        if (msg.sender != owner) revert OnlyOwner();
        _;
    }

    // ── External: initiate liquidation ────────────────────────────────────
    /**
     * @param collateralAsset   Asset we will receive as collateral
     * @param debtAsset         Asset we must repay on behalf of the user
     * @param userToLiquidate   Borrower address
     * @param debtAmount        Exact amount to cover (≤ 50% of user's debt)
     * @param swapFee           Uniswap V3 fee tier for single-hop swap
     * @param swapPath          Encoded multi-hop path (leave empty for single hop)
     * @param minProfitWei      Revert if profit (in debtAsset) is below this
     */
    function executeLiquidation(
        address collateralAsset,
        address debtAsset,
        address userToLiquidate,
        uint256 debtAmount,
        uint24  swapFee,
        bytes calldata swapPath,
        uint256 minProfitWei
    ) external onlyOwner {
        bytes memory params = abi.encode(
            LiqParams({
                collateralAsset:  collateralAsset,
                debtAsset:        debtAsset,
                userToLiquidate:  userToLiquidate,
                debtToCover:      debtAmount,
                swapFee:          swapFee,
                swapPath:         swapPath,
                minProfitWei:     minProfitWei
            })
        );
        POOL.flashLoanSimple(address(this), debtAsset, debtAmount, params, 0);
    }

    // ── Aave flash loan callback ───────────────────────────────────────────
    /**
     * @notice Called by Aave pool after sending flash loan funds.
     *         Must return true and have repayAmount approved before returning.
     */
    function executeOperation(
        address asset,      // debtAsset (same as what we borrowed)
        uint256 amount,     // borrowed amount
        uint256 premium,    // 0.05% flash loan fee
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        if (msg.sender   != address(POOL)) revert OnlyPool();
        if (initiator    != address(this)) revert OnlySelf();

        LiqParams memory lp = abi.decode(params, (LiqParams));

        // ── 1. Approve Aave to pull debtAsset for the liquidation call ────
        IERC20(asset).approve(address(POOL), amount);

        // ── 2. Execute liquidation ────────────────────────────────────────
        uint256 collBefore = IERC20(lp.collateralAsset).balanceOf(address(this));
        POOL.liquidationCall(
            lp.collateralAsset,
            lp.debtAsset,
            lp.userToLiquidate,
            lp.debtToCover,
            false   // receive underlying token, not aToken
        );
        uint256 collReceived = IERC20(lp.collateralAsset).balanceOf(address(this)) - collBefore;
        if (collReceived == 0) revert NoCollateralReceived();

        // ── 3. Swap collateral → debtAsset ───────────────────────────────
        uint256 debtReceived;
        if (lp.collateralAsset == lp.debtAsset) {
            // Same asset (rare): no swap needed
            debtReceived = collReceived;
        } else if (lp.swapPath.length > 0) {
            debtReceived = _multiHopSwap(lp.collateralAsset, collReceived, lp.swapPath);
        } else {
            debtReceived = _singleHopSwap(lp.collateralAsset, lp.debtAsset, collReceived, lp.swapFee);
        }

        // ── 4. Repay flash loan ───────────────────────────────────────────
        uint256 repayAmount = amount + premium;
        IERC20(asset).approve(address(POOL), repayAmount);

        // ── 5. Profit check ───────────────────────────────────────────────
        uint256 profit = debtReceived > repayAmount ? debtReceived - repayAmount : 0;
        if (profit < lp.minProfitWei) revert InsufficientProfit(profit, lp.minProfitWei);

        emit LiquidationExecuted(
            lp.userToLiquidate,
            lp.collateralAsset,
            lp.debtAsset,
            amount,
            collReceived,
            profit
        );

        return true;
    }

    // ── Internal: swap helpers ─────────────────────────────────────────────
    function _singleHopSwap(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint24  fee
    ) internal returns (uint256 amountOut) {
        IERC20(tokenIn).approve(address(SWAP_ROUTER), amountIn);
        amountOut = SWAP_ROUTER.exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn:           tokenIn,
                tokenOut:          tokenOut,
                fee:               fee,
                recipient:         address(this),
                deadline:          block.timestamp + 120,
                amountIn:          amountIn,
                amountOutMinimum:  0,   // checked by minProfitWei above
                sqrtPriceLimitX96: 0
            })
        );
    }

    function _multiHopSwap(
        address tokenIn,
        uint256 amountIn,
        bytes memory path
    ) internal returns (uint256 amountOut) {
        IERC20(tokenIn).approve(address(SWAP_ROUTER), amountIn);
        amountOut = SWAP_ROUTER.exactInput(
            ISwapRouter.ExactInputParams({
                path:             path,
                recipient:        address(this),
                deadline:         block.timestamp + 120,
                amountIn:         amountIn,
                amountOutMinimum: 0
            })
        );
    }

    // ── Owner utilities ────────────────────────────────────────────────────
    /// @notice Withdraw a specific token balance to owner
    function withdraw(address token, uint256 amount) external onlyOwner {
        IERC20(token).transfer(owner, amount);
        emit Withdrawn(token, amount, owner);
    }

    /// @notice Sweep entire balance of a token to owner
    function withdrawAll(address token) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        IERC20(token).transfer(owner, bal);
        emit Withdrawn(token, bal, owner);
    }

    /// @notice Rescue ETH accidentally sent to contract
    function withdrawETH() external onlyOwner {
        payable(owner).transfer(address(this).balance);
    }

    /// @notice Transfer ownership (two-step recommended for production)
    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        owner = newOwner;
    }

    receive() external payable {}
}
