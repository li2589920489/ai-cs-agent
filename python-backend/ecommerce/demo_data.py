"""
模拟电商数据 — 模拟一个零食/日用品淘宝店铺的完整数据
"""

# ==================== 商品库 ====================

PRODUCT_CATALOG = [
    {
        "product_id": "P1001",
        "name": "良品铺子坚果大礼包 1200g",
        "price": "89.00",
        "original_price": "129.00",
        "category": "零食",
        "stock": {
            "经典款": 320,
            "豪华款": 85,
        },
        "description": "6种坚果混装：巴旦木、腰果、夏威夷果、核桃、榛子、开心果",
        "specs": ["经典款（6袋装）", "豪华款（10袋装）"],
        "rating": 4.8,
        "reviews": 12680,
        "free_shipping": True,
    },
    {
        "product_id": "P1002",
        "name": "三只松鼠每日坚果 750g",
        "price": "69.90",
        "original_price": "99.00",
        "category": "零食",
        "stock": {
            "30袋装": 150,
            "15袋装": 420,
        },
        "description": "每日一小袋，科学配比，独立包装锁鲜",
        "specs": ["30袋装", "15袋装"],
        "rating": 4.9,
        "reviews": 8950,
        "free_shipping": True,
    },
    {
        "product_id": "P1003",
        "name": "小米加湿器 4L 大容量",
        "price": "79.00",
        "original_price": "129.00",
        "category": "家电",
        "stock": {
            "白色": 200,
            "绿色": 120,
        },
        "description": "4L大容量持续加湿16小时，静音设计28dB，缺水自动断电",
        "specs": ["白色 4L", "绿色 4L"],
        "rating": 4.6,
        "reviews": 5200,
        "free_shipping": False,
        "shipping_fee": "8.00",
    },
    {
        "product_id": "P1004",
        "name": "维达抽纸 3层 120抽×24包",
        "price": "49.90",
        "original_price": "69.90",
        "category": "日用品",
        "stock": {
            "3层120抽": 580,
        },
        "description": "原生木浆，3层加厚，无荧光剂，母婴可用",
        "specs": ["3层120抽×24包"],
        "rating": 4.7,
        "reviews": 21500,
        "free_shipping": True,
    },
]

# ==================== 订单库（模拟已存在的订单） ====================

MOCK_ORDERS = {
    "TB20260812001": {
        "order_number": "TB20260812001",
        "customer_name": "张小明",
        "account_id": "U8842",
        "items": [
            {"product_id": "P1001", "name": "良品铺子坚果大礼包 1200g", "sku": "经典款", "price": "89.00", "quantity": 2},
            {"product_id": "P1004", "name": "维达抽纸 3层 120抽×24包", "sku": "3层120抽", "price": "49.90", "quantity": 1},
        ],
        "total_amount": "227.90",
        "paid_amount": "207.90",
        "coupon_used": "满200减20",
        "status": "已发货",
        "tracking": {"company": "圆通速递", "number": "YT202608120009876", "status": "运输中", "location": "已到达【北京分拨中心】", "estimated_delivery": "2026-08-14"},
        "order_time": "2026-08-12 09:32:15",
        "payment_time": "2026-08-12 09:32:28",
        "shipping_address": "北京市朝阳区望京街道XX小区3号楼1单元502",
    },
    "TB20260810002": {
        "order_number": "TB20260810002",
        "customer_name": "张小明",
        "account_id": "U8842",
        "items": [
            {"product_id": "P1002", "name": "三只松鼠每日坚果 750g", "sku": "30袋装", "price": "69.90", "quantity": 1},
        ],
        "total_amount": "69.90",
        "paid_amount": "69.90",
        "coupon_used": None,
        "status": "已签收",
        "tracking": {"company": "韵达快递", "number": "YD202608100005432", "status": "已签收", "location": "已签收，签收人：本人", "estimated_delivery": "2026-08-11"},
        "order_time": "2026-08-10 14:20:33",
        "payment_time": "2026-08-10 14:20:45",
        "shipping_address": "北京市朝阳区望京街道XX小区3号楼1单元502",
    },
    "TB20260808003": {
        "order_number": "TB20260808003",
        "customer_name": "张小明",
        "account_id": "U8842",
        "items": [
            {"product_id": "P1003", "name": "小米加湿器 4L", "sku": "白色 4L", "price": "79.00", "quantity": 1},
        ],
        "total_amount": "87.00",
        "paid_amount": "87.00",
        "coupon_used": None,
        "status": "已签收",
        "tracking": {"company": "中通快递", "number": "ZT202608080001234", "status": "已签收", "location": "已签收，签收人：家人代收", "estimated_delivery": "2026-08-10"},
        "order_time": "2026-08-08 20:15:00",
        "payment_time": "2026-08-08 20:15:12",
        "shipping_address": "北京市朝阳区望京街道XX小区3号楼1单元502",
    },
}

# ==================== 店铺政策（FAQ / RAG知识库内容） ====================

STORE_POLICIES = {
    "退货政策": """
退货政策：
1. 自签收之日起7天内可申请无理由退货，商品需保持原包装完好、不影响二次销售。
2. 食品类商品（坚果、零食等）拆封后不支持无理由退货，如有质量问题请联系客服处理。
3. 退货流程：在「我的订单」中点击「申请退货」→ 填写退货原因 → 等待审核（24小时内）→ 审核通过后按照指引寄回商品 → 仓库签收后1-3个工作日退款到原支付账户。
4. 退货产生的运费：因商品质量问题导致的退货，运费由本店承担；非质量问题的无理由退货，运费由买家承担。
5. 换货：仅支持同款商品换货（同商品不同规格可换），流程与退货相同。
""",

    "退款时效": """
退款时效说明：
1. 退货退款：仓库收到退回商品后1-3个工作日内退款到原支付账户。
2. 仅退款未发货：申请后24小时内审核退款。
3. 售后维权退款：需人工审核，处理时效为1-5个工作日。
4. 退款到账时间取决于支付方式：支付宝/微信支付通常即时到账，银行卡可能需要1-3个工作日。
""",

    "发货时效": """
发货时效说明：
1. 现货商品：工作日下午16:00前下单，当天发货；16:00后下单次日发货。
2. 预售商品：以商品页面标注的发货时间为准。
3. 节假日发货以店铺公告为准，春节、国庆等长假期间发货可能延迟。
4. 如遇不可抗力（自然灾害、疫情封控等）导致延迟发货，会通过短信通知。
""",

    "物流查询": """
物流查询方法：
1. 在「我的订单」中找到对应订单，点击「查看物流」即可查看实时物流信息。
2. 物流信息一般在下单后24小时内更新，如48小时未更新请联系客服。
3. 如物流显示已签收但未收到货，请先确认是否家人/同事/物业代收，确认无代人签收后联系客服。
4. 物流出现异常（退回、丢件等），客服会协助联系快递公司处理，处理时效一般为24-72小时。
""",

    "优惠券规则": """
优惠券使用规则：
1. 店铺优惠券：满200减20、满500减60、满1000减150。
2. 新用户专享：首单满50减10（限首次下单用户）。
3. 优惠券不可叠加使用，每笔订单限用一张。
4. 优惠券有效期以领取页面显示为准，过期自动失效。
5. 使用优惠券的订单如发生退款，优惠券不予退还。
6. 会员等级优惠与优惠券可叠加：银卡会员额外9.5折、金卡9折、钻石卡8.5折。
""",

    "会员权益": """
会员等级及权益：
1. 普通会员：注册即享，累积消费。
2. 银卡会员（年消费满500元）：全场9.5折 + 生日月双倍积分。
3. 金卡会员（年消费满2000元）：全场9折 + 优先发货 + 专属客服。
4. 钻石卡会员（年消费满5000元）：全场8.5折 + 免运费 + 7天无理由延长至15天 + 专属客服经理。
""",

    "常见售后问题": """
常见售后问题处理：
1. 收到商品破损：请在签收24小时内拍照联系客服，我们会安排补发或退款。
2. 商品与描述不符：拍照联系客服核实，确认后安排退货退款（运费本店承担）。
3. 少发/漏发：联系客服提供订单号和收到的商品照片，核实后补发。
4. 商品质量问题：拍照/视频联系客服，核实后安排退换货（运费本店承担）。
5. 食品类商品如存在异物、变质等问题，请保留商品和包装，拍照联系客服，我们会严肃处理并给予补偿。
""",
}

# ==================== 优惠券库 ====================

AVAILABLE_COUPONS = [
    {"code": "NEW10", "type": "新用户专享", "discount": "满50减10", "status": "可用", "expire_date": "2026-12-31"},
    {"code": "FULL200", "type": "满减券", "discount": "满200减20", "status": "可用", "expire_date": "2026-08-31"},
    {"code": "FULL500", "type": "满减券", "discount": "满500减60", "status": "可用", "expire_date": "2026-08-31"},
    {"code": "FULL1000", "type": "满减券", "discount": "满1000减150", "status": "可用", "expire_date": "2026-08-31"},
]


def get_product(product_id: str) -> dict | None:
    """根据商品ID查找商品"""
    for p in PRODUCT_CATALOG:
        if p["product_id"] == product_id:
            return p
    return None


def get_order(order_number: str) -> dict | None:
    """根据订单号查找订单"""
    return MOCK_ORDERS.get(order_number)


def get_customer_orders(account_id: str) -> list[dict]:
    """获取某用户的所有订单"""
    return [o for o in MOCK_ORDERS.values() if o["account_id"] == account_id]


def search_products(keyword: str) -> list[dict]:
    """根据关键词搜索商品"""
    k = keyword.lower()
    return [p for p in PRODUCT_CATALOG if k in p["name"].lower() or k in p.get("description", "").lower() or k in p["category"].lower()]
