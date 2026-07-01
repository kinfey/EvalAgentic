import os
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request


app = Flask(__name__)

DEFAULT_PORT = 8082
DEFAULT_PAGE_SIZE = int(os.environ.get("DEFAULT_PAGE_SIZE", "8"))
MAX_PAGE_SIZE = int(os.environ.get("MAX_PAGE_SIZE", "24"))

CATEGORY_META = {
    "Digital": ["Wireless", "Smart", "Fast delivery"],
    "Fashion": ["New season", "Free shipping", "Popular"],
    "Home": ["Comfort", "Practical", "Coupon"],
    "Beauty": ["Daily care", "Hot sale", "Gift pick"],
    "Sports": ["Outdoor", "Lightweight", "Best seller"],
    "Toys": ["Parent pick", "Creative", "Safe material"],
}


def _goods_seed():
    base_time = datetime(2026, 6, 1, 9, 30)
    raw_goods = [
        ("Wireless Bluetooth Earbuds Pro", 199.00, "Digital", 8621, "Active noise cancellation, long battery life, and low-latency gaming mode."),
        ("87-Key RGB Mechanical Keyboard", 359.00, "Digital", 4210, "Hot-swappable switches, compact layout, and vibrant RGB effects."),
        ("Loose Cotton Hoodie", 89.00, "Fashion", 15330, "Soft cotton hoodie with relaxed fit for daily streetwear styling."),
        ("Breathable Running Shoes", 239.00, "Fashion", 7302, "Lightweight running shoes with cushioned midsole and breathable knit upper."),
        ("Ergonomic Office Chair", 899.00, "Home", 1934, "Adjustable lumbar support, breathable mesh back, and smooth rolling casters."),
        ("Cotton Four-Piece Bedding Set", 299.00, "Home", 5208, "Skin-friendly cotton bedding set with duvet cover, sheet, and pillowcases."),
        ("USB-C 8-in-1 Multiport Dock", 159.00, "Digital", 6288, "HDMI, USB, SD card, and fast charging ports for laptops and tablets."),
        ("316 Stainless Steel Thermal Mug", 59.00, "Home", 18920, "Leak-proof thermal mug that keeps drinks hot or cold for hours."),
        ("Kids Building Block Set", 69.00, "Toys", 9660, "Colorful building blocks for creative play and early STEM learning."),
        ("Non-stick Induction Frying Pan", 79.00, "Home", 7856, "Durable non-stick pan suitable for gas, electric, and induction cooktops."),
        ("Textured Cropped Jacket", 188.00, "Fashion", 3448, "Modern cropped jacket with textured fabric and easy-match silhouette."),
        ("20000mAh Fast-Charge Power Bank", 129.00, "Digital", 11032, "High-capacity portable charger with dual USB output and USB-C input."),
        ("Hydrating Amino Acid Cleanser", 49.90, "Beauty", 12880, "Gentle facial cleanser for daily use with a refreshing soft foam."),
        ("Matte Long-Wear Lip Glaze", 39.90, "Beauty", 22310, "High-pigment lip glaze with comfortable matte finish and lasting color."),
        ("Foldable Yoga Mat", 99.00, "Sports", 4812, "Non-slip yoga mat with foldable design for home workouts and travel."),
        ("Adjustable Dumbbell Pair", 269.00, "Sports", 3126, "Space-saving dumbbells with quick weight adjustment for strength training."),
        ("Smart LED Desk Lamp", 139.00, "Home", 6840, "Eye-care LED desk lamp with touch dimming and adjustable color temperature."),
        ("Portable Mini Projector", 699.00, "Digital", 2650, "Compact projector for home cinema, camping, and bedroom entertainment."),
        ("High-Waist Straight Jeans", 119.00, "Fashion", 8144, "Classic high-waist jeans with straight-leg cut and comfortable denim."),
        ("Plush Cat Paw Pillow", 45.00, "Toys", 6721, "Soft plush pillow with cute cat paw design for sofa or bedroom decor."),
        ("Aromatherapy Humidifier", 109.00, "Home", 5905, "Quiet humidifier with night light and essential oil aroma tray."),
        ("Smart Fitness Band", 169.00, "Digital", 9052, "Tracks heart rate, sleep, steps, workouts, and phone notifications."),
        ("Sun Protection Baseball Cap", 35.00, "Fashion", 17105, "Lightweight cap with curved brim for casual outdoor sun protection."),
        ("Waterproof Hiking Backpack", 149.00, "Sports", 4386, "Large-capacity backpack with waterproof fabric and multiple compartments."),
    ]

    goods = []
    for index, (name, price, category, sales_count, description) in enumerate(raw_goods, start=1):
        goods.append(
            {
                "id": index,
                "name": name,
                "price": price,
                "image_url": f"https://picsum.photos/seed/taobao-good-{index}/600/600",
                "description": description,
                "category": category,
                "sales_count": sales_count,
                "created_at": (base_time + timedelta(hours=index * 7)).isoformat(timespec="seconds"),
                "shop": f"{category} Flagship Store",
                "location": ["Hangzhou", "Shanghai", "Shenzhen", "Guangzhou", "Chengdu", "Beijing"][index % 6],
                "tags": CATEGORY_META[category],
            }
        )
    return goods


GOODS = _goods_seed()


def _json_response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Access-Control-Allow-Origin"] = os.environ.get("CORS_ORIGIN", "*")
    return response


def _positive_int_arg(name, default, maximum=None):
    raw_value = request.args.get(name, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = default
    value = max(1, value)
    return min(value, maximum) if maximum else value


def _filtered_goods():
    keyword = request.args.get("q", "").strip().lower()
    category = request.args.get("category", "").strip()
    sort = request.args.get("sort", "recommended").strip()

    goods = GOODS
    if keyword:
        goods = [
            item
            for item in goods
            if keyword in item["name"].lower()
            or keyword in item["description"].lower()
            or keyword in item["category"].lower()
            or keyword in item["shop"].lower()
        ]

    if category:
        goods = [item for item in goods if item["category"] == category]

    sorters = {
        "price_asc": lambda item: (item["price"], -item["sales_count"]),
        "price_desc": lambda item: (-item["price"], -item["sales_count"]),
        "sales_desc": lambda item: (-item["sales_count"], item["price"]),
        "sales_count_desc": lambda item: (-item["sales_count"], item["price"]),
        "newest": lambda item: item["created_at"],
    }
    if sort in sorters:
        reverse = sort == "newest"
        goods = sorted(goods, key=sorters[sort], reverse=reverse)

    return goods


@app.get("/")
def index():
    return render_template("index.html", port=os.environ.get("PORT", str(DEFAULT_PORT)), detail_id=None)


@app.get("/goods/<int:good_id>")
def goods_detail_page(good_id):
    return render_template("index.html", port=os.environ.get("PORT", str(DEFAULT_PORT)), detail_id=good_id)


@app.get("/api/goods")
def list_goods():
    goods = _filtered_goods()
    page = _positive_int_arg("page", 1)
    per_page = _positive_int_arg("per_page", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    total = len(goods)
    start = (page - 1) * per_page
    end = start + per_page

    return _json_response(
        {
            "goods": goods[start:end],
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": (total + per_page - 1) // per_page,
                "has_next": end < total,
            },
        }
    )


@app.get("/api/goods/<int:good_id>")
def get_good(good_id):
    good = next((item for item in GOODS if item["id"] == good_id), None)
    if good is None:
        return _json_response({"error": "Good not found"}, 404)
    return _json_response(good)


@app.get("/api/categories")
def list_categories():
    categories = sorted({item["category"] for item in GOODS})
    return _json_response({"categories": categories})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    app.run(host="0.0.0.0", port=port)
