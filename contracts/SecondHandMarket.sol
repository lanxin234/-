// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title 区块链二手交易平台智能合约
 * @notice 校园二手交易 + 资金托管 + 所有权转移 + 信誉评价
 *
 * 核心设计思想：
 *   传统二手交易的问题 —— 买家怕付了钱卖家不发货，卖家怕发了货买家不付钱。
 *   本合约用「资金托管」解决这个信任问题：
 *     买家下单 → 钱进合约（不是给卖家）→ 确认收货 → 合约才放款给卖家
 *   全程无需信任对方，也不需要平台做中间人。
 */
contract SecondHandMarket {

    // ============================================================
    //  数据结构
    // ============================================================

    /// @dev 商品状态枚举。用数字表示状态，节省链上存储
    enum ItemStatus {
        OnSale,     // 0 - 在售
        InEscrow,   // 1 - 交易中（钱在托管，等收货）
        Sold,       // 2 - 已售出
        Removed     // 3 - 已下架
    }

    /// @dev 商品结构体
    struct Item {
        uint256 id;             // 商品ID
        string  name;           // 商品名称
        string  description;    // 商品描述
        uint256 price;          // 价格（单位：wei）
        address payable seller; // 卖家地址
        address buyer;          // 买家地址（交易中才有值）
        ItemStatus status;      // 当前状态
        uint256 createdAt;      // 上架时间戳
    }

    /// @dev 用户信誉结构体
    struct User {
        string  nickname;       // 昵称
        uint256 soldCount;      // 成功卖出次数
        uint256 boughtCount;    // 成功买入次数
        uint256 ratingSum;      // 收到的评分总和
        uint256 ratingCount;    // 收到评分次数
        bool    registered;     // 是否已注册
    }

    // ============================================================
    //  状态变量
    // ============================================================

    address public owner;                        // 合约部署者（平台方）
    uint256 public itemCounter;                  // 商品计数器，每上架一件 +1
    uint256 public platformFeeRate = 100;        // 平台手续费率 = 1%（100 / 10000）

    mapping(uint256 => Item) public items;       // 商品ID => 商品
    mapping(address => User) public users;       // 地址 => 用户信息
    mapping(uint256 => mapping(address => bool)) public hasRated; // 商品ID => 地址 => 是否已评价

    // ============================================================
    //  事件（前端和区块链浏览器靠事件感知链上发生了什么）
    // ============================================================

    event UserRegistered(address indexed user, string nickname);
    event ItemListed(uint256 indexed itemId, address indexed seller, string name, uint256 price);
    event ItemPurchased(uint256 indexed itemId, address indexed buyer, uint256 amount);
    event ItemDelivered(uint256 indexed itemId, address indexed buyer);
    event ItemRemoved(uint256 indexed itemId, address indexed seller);
    event Rated(address indexed from, address indexed to, uint256 indexed itemId, uint8 score);
    event Refunded(uint256 indexed itemId, address indexed buyer, uint256 amount);

    // ============================================================
    //  修饰器（权限检查，减少重复代码）
    // ============================================================

    /// @dev 只允许已注册用户操作
    modifier onlyRegistered() {
        require(users[msg.sender].registered, "Please register first");
        _;
    }

    /// @dev 商品必须存在
    modifier itemExists(uint256 _itemId) {
        require(_itemId > 0 && _itemId <= itemCounter, "Item does not exist");
        _;
    }

    // ============================================================
    //  构造函数
    // ============================================================

    constructor() {
        owner = msg.sender;
    }

    // ============================================================
    //  用户模块
    // ============================================================

    /**
     * @notice 注册账号
     * @param _nickname 昵称
     */
    function register(string memory _nickname) public {
        require(!users[msg.sender].registered, "Already registered");
        require(bytes(_nickname).length > 0, "Nickname cannot be empty");
        require(bytes(_nickname).length <= 20, "Nickname too long (max 20)");

        users[msg.sender] = User({
            nickname: _nickname,
            soldCount: 0,
            boughtCount: 0,
            ratingSum: 0,
            ratingCount: 0,
            registered: true
        });

        emit UserRegistered(msg.sender, _nickname);
    }

    /**
     * @notice 查询用户信息
     * @return nickname 昵称
     * @return soldCount 卖出数
     * @return boughtCount 买入数
     * @return avgRating 平均分（乘100，避免小数；如 450 表示 4.5 分）
     */
    function getUserInfo(address _user)
        public
        view
        returns (string memory nickname, uint256 soldCount, uint256 boughtCount, uint256 avgRating)
    {
        User storage u = users[_user];
        uint256 avg = u.ratingCount == 0 ? 0 : (u.ratingSum * 100) / u.ratingCount;
        return (u.nickname, u.soldCount, u.boughtCount, avg);
    }

    // ============================================================
    //  商品模块
    // ============================================================

    /**
     * @notice 上架商品
     * @param _name 商品名称
     * @param _description 商品描述
     * @param _price 价格（单位 wei）
     */
    function listItem(string memory _name, string memory _description, uint256 _price)
        public
        onlyRegistered
    {
        require(bytes(_name).length > 0, "Item name cannot be empty");
        require(bytes(_name).length <= 50, "Item name too long (max 50)");
        require(_price > 0, "Price must be greater than 0");

        itemCounter++;

        items[itemCounter] = Item({
            id: itemCounter,
            name: _name,
            description: _description,
            price: _price,
            seller: payable(msg.sender),
            buyer: address(0),
            status: ItemStatus.OnSale,
            createdAt: block.timestamp
        });

        emit ItemListed(itemCounter, msg.sender, _name, _price);
    }

    /**
     * @notice 买家下单 —— 钱进入合约托管，不直接给卖家
     * @dev 这是整个合约最核心的函数，体现了「担保交易」的思想
     */
    function buyItem(uint256 _itemId) public payable onlyRegistered itemExists(_itemId) {
        Item storage item = items[_itemId];

        require(item.status == ItemStatus.OnSale, "Item is not on sale");
        require(item.seller != msg.sender, "Cannot buy your own item");
        require(msg.value == item.price, "Payment amount does not match price");

        // 记录买家和状态，钱留在合约里（托管）
        item.buyer = msg.sender;
        item.status = ItemStatus.InEscrow;

        emit ItemPurchased(_itemId, msg.sender, msg.value);
    }

    /**
     * @notice 买家确认收货 —— 合约放款给卖家，所有权正式转移
     * @dev 只有买家能调用，钱从合约转给卖家，扣除平台手续费
     */
    function confirmReceived(uint256 _itemId) public itemExists(_itemId) {
        Item storage item = items[_itemId];

        require(item.status == ItemStatus.InEscrow, "Item is not in escrow");
        require(item.buyer == msg.sender, "Only the buyer can confirm receipt");

        uint256 fee = (item.price * platformFeeRate) / 10000;
        uint256 sellerAmount = item.price - fee;

        // 状态先改，再转账（防止重入攻击）
        item.status = ItemStatus.Sold;
        users[item.seller].soldCount++;
        users[item.buyer].boughtCount++;

        // 放款
        item.seller.transfer(sellerAmount);
        if (fee > 0 && owner != address(0)) {
            payable(owner).transfer(fee);
        }

        emit ItemDelivered(_itemId, msg.sender);
    }

    /**
     * @notice 卖家下架商品（仅在售状态可下架）
     */
    function removeItem(uint256 _itemId) public itemExists(_itemId) {
        Item storage item = items[_itemId];

        require(item.seller == msg.sender, "Only the seller can remove the item");
        require(item.status == ItemStatus.OnSale, "Only items on sale can be removed");

        item.status = ItemStatus.Removed;

        emit ItemRemoved(_itemId, msg.sender);
    }

    /**
     * @notice 查询商品详情
     */
    function getItem(uint256 _itemId)
        public
        view
        itemExists(_itemId)
        returns (
            uint256 id,
            string memory name,
            string memory description,
            uint256 price,
            address seller,
            address buyer,
            ItemStatus status,
            uint256 createdAt
        )
    {
        Item storage item = items[_itemId];
        return (
            item.id,
            item.name,
            item.description,
            item.price,
            item.seller,
            item.buyer,
            item.status,
            item.createdAt
        );
    }

    /**
     * @notice 获取商品总数
     */
    function getItemCount() public view returns (uint256) {
        return itemCounter;
    }

    // ============================================================
    //  信誉评价模块
    // ============================================================

    /**
     * @notice 评价交易对方
     * @param _itemId 商品ID
     * @param _to 被评价人
     * @param _score 评分（1-5 星）
     * @dev 只有交易双方能评价，且每笔交易每人只能评一次
     */
    function rate(uint256 _itemId, address _to, uint8 _score) public itemExists(_itemId) {
        Item storage item = items[_itemId];

        require(item.status == ItemStatus.Sold, "Can only rate after the deal is completed");
        require(_score >= 1 && _score <= 5, "Score must be between 1 and 5");
        require(msg.sender == item.buyer || msg.sender == item.seller, "Only the two parties can rate");
        require(_to == item.buyer || _to == item.seller, "Can only rate the counterparty");
        require(_to != msg.sender, "Cannot rate yourself");
        require(!hasRated[_itemId][msg.sender], "You have already rated");

        hasRated[_itemId][msg.sender] = true;
        users[_to].ratingSum += _score;
        users[_to].ratingCount++;

        emit Rated(msg.sender, _to, _itemId, _score);
    }

    // ============================================================
    //  平台管理
    // ============================================================

    /// @notice 修改平台手续费率（仅平台方可调用）
    function setPlatformFee(uint256 _rate) public {
        require(msg.sender == owner, "Only the platform owner can modify");
        require(_rate <= 500, "Platform fee cannot exceed 5%");
        platformFeeRate = _rate;
    }

    /// @notice 查询合约当前托管的资金总额
    function getEscrowBalance() public view returns (uint256) {
        return address(this).balance;
    }
}
