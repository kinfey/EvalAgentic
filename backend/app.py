import os

from flask import Flask, jsonify, render_template


app = Flask(__name__)

GOODS = [
    {"id": 1, "title": "无线蓝牙耳机 Pro", "price": 199.00, "image_url": "https://picsum.photos/seed/taobao-1/500/500", "description": "主动降噪，40 小时长续航，通勤游戏都适合。"},
    {"id": 2, "title": "RGB 机械键盘 87 键", "price": 359.00, "image_url": "https://picsum.photos/seed/taobao-2/500/500", "description": "热插拔茶轴，PBT 键帽，多系统兼容。"},
    {"id": 3, "title": "春秋宽松连帽卫衣", "price": 89.00, "image_url": "https://picsum.photos/seed/taobao-3/500/500", "description": "亲肤棉质面料，男女同款，日常百搭。"},
    {"id": 4, "title": "人体工学电脑椅", "price": 899.00, "image_url": "https://picsum.photos/seed/taobao-4/500/500", "description": "头枕腰托可调节，久坐办公更舒适。"},
    {"id": 5, "title": "USB-C 多功能扩展坞", "price": 159.00, "image_url": "https://picsum.photos/seed/taobao-5/500/500", "description": "8 合 1 接口，支持 HDMI、PD 快充和千兆网口。"},
    {"id": 6, "title": "20000mAh 快充充电宝", "price": 129.00, "image_url": "https://picsum.photos/seed/taobao-6/500/500", "description": "双向快充，大容量电芯，旅行通勤备用电源。"},
    {"id": 7, "title": "家用不粘炒锅", "price": 79.00, "image_url": "https://picsum.photos/seed/taobao-7/500/500", "description": "少油烟不粘涂层，燃气灶电磁炉通用。"},
    {"id": 8, "title": "轻便透气跑步鞋", "price": 239.00, "image_url": "https://picsum.photos/seed/taobao-8/500/500", "description": "缓震回弹中底，透气网面，适合跑步健身。"},
    {"id": 9, "title": "全棉四件套", "price": 299.00, "image_url": "https://picsum.photos/seed/taobao-9/500/500", "description": "A 类全棉面料，柔软透气，简约纯色。"},
    {"id": 10, "title": "儿童益智积木套装", "price": 69.00, "image_url": "https://picsum.photos/seed/taobao-10/500/500", "description": "大颗粒安全积木，锻炼动手和空间想象力。"},
    {"id": 11, "title": "316 不锈钢保温杯", "price": 59.00, "image_url": "https://picsum.photos/seed/taobao-11/500/500", "description": "长效保温保冷，杯盖防漏，便携耐用。"},
    {"id": 12, "title": "法式小香风外套", "price": 188.00, "image_url": "https://picsum.photos/seed/taobao-12/500/500", "description": "短款显高版型，细腻编织纹理，秋冬新款。"},
]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/goods")
def list_goods():
    return jsonify({"goods": GOODS})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8082"))
    app.run(host="0.0.0.0", port=port)
