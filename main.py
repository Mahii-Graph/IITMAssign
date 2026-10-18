import os
import re
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Ledger Agent")
IST = ZoneInfo("Asia/Kolkata")

EXPORT_URL = os.getenv("DATA_EXPORT_URL", "").strip()
RATES_URL = os.getenv("RATES_URL", "").strip()

orders = []
rates = {"USD": 1.0, "EUR": 1.11, "INR": 0.01174}
load_error = None


def parse_dt(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=IST)
        return d.astimezone(IST)
    except Exception:
        return None


def load_data():
    global orders, rates, load_error
    if not EXPORT_URL or not RATES_URL:
        load_error = "Set DATA_EXPORT_URL and RATES_URL environment variables."
        return

    try:
        r = requests.get(EXPORT_URL, timeout=45)
        r.raise_for_status()
        raw = r.text.strip()
        rows = []
        # The export endpoint returns one JSON object per line.
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and "id" in obj:
                    rows.append(obj)
            except json.JSONDecodeError:
                continue

        rr = requests.get(RATES_URL, timeout=20)
        rr.raise_for_status()
        rd = rr.json()
        new_rates = rd.get("usd_per_unit", {})
        if new_rates:
            rates = {str(k).upper(): float(v) for k, v in new_rates.items()}

        # For duplicate IDs, keep the row with the latest updated_at.
        latest = {}
        for row in rows:
            oid = str(row.get("id", ""))
            old = latest.get(oid)
            nd = parse_dt(row.get("updated_at"))
            od = parse_dt(old.get("updated_at")) if old else None
            if old is None or (nd and (od is None or nd > od)):
                latest[oid] = row

        orders = list(latest.values())
        load_error = None
    except Exception as e:
        load_error = f"{type(e).__name__}: {e}"


@app.on_event("startup")
def startup():
    load_data()


class Question(BaseModel):
    question: str


def money_usd(row):
    cur = str(row.get("currency", "USD")).upper()
    return float(row.get("amount", 0) or 0) * rates.get(cur, 1.0)


def target_currency(q):
    q = q.lower()
    # Rates are USD value of one unit. Convert source -> USD -> target.
    if re.search(r"\b(inr|rupees?|₹)\b", q):
        return "INR"
    if re.search(r"\beur|euros?|€\b", q):
        return "EUR"
    return "USD"


def format_money(value, cur):
    symbols = {"USD": "$", "EUR": "€", "INR": "₹"}
    return f"{symbols.get(cur, cur + ' ')}{value:,.2f}"


def convert_from_usd(value, cur):
    rate = rates.get(cur, 1.0)
    return value / rate if rate else value


def month_bounds(q):
    months = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12
    }
    low = q.lower()
    for name, m in months.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            ym = re.search(r"\b(20\d{2})\b", low)
            year = int(ym.group(1)) if ym else None
            return m, year
    return None, None


def filter_orders(q, base):
    selected = list(base)
    low = q.lower()

    # Date/month filters use created_at interpreted in Asia/Kolkata.
    m, y = month_bounds(low)
    year_match = re.search(r"\b(20\d{2})\b", low)
    year = y or (int(year_match.group(1)) if year_match else None)
    if m:
        selected = [o for o in selected if (parse_dt(o.get("created_at")) and
                    parse_dt(o.get("created_at")).month == m and
                    (year is None or parse_dt(o.get("created_at")).year == year))]
    elif year:
        selected = [o for o in selected if parse_dt(o.get("created_at")) and
                    parse_dt(o.get("created_at")).year == year]

    # Region filters.
    for region in ("north", "south", "east", "west", "central"):
        if re.search(r"\b" + region + r"\b", low):
            selected = [o for o in selected if str(o.get("region", "")).lower() == region]
            break

    # Exact product names found in the dataset.
    products = sorted({str(o.get("product", "")) for o in orders if o.get("product")}, key=len, reverse=True)
    for product in products:
        if product.lower() in low:
            selected = [o for o in selected if str(o.get("product", "")).lower() == product.lower()]
            break

    customer_match = re.search(r"\bC\d+\b", q, re.I)
    if customer_match:
        cid = customer_match.group(0).upper()
        selected = [o for o in selected if str(o.get("customer", "")).upper() == cid]

    return selected


def answer_question(q):
    if load_error:
        raise HTTPException(status_code=503, detail="Ledger data could not be loaded. Check Render environment variables and logs.")
    if not orders:
        raise HTTPException(status_code=503, detail="No ledger rows were loaded.")

    low = q.lower()
    # Refunds are a separate category; otherwise revenue means paid only.
    if "refund" in low or "refunded" in low:
        base = [o for o in orders if str(o.get("status", "")).lower() == "refunded"]
        kind = "refund"
    elif "void" in low or "cancelled" in low or "canceled" in low:
        base = [o for o in orders if str(o.get("status", "")).lower() == "void"]
        kind = "void"
    else:
        base = [o for o in orders if str(o.get("status", "")).lower() == "paid"]
        kind = "revenue"

    rows = filter_orders(q, base)
    cur = target_currency(q)
    total_usd = sum(money_usd(o) for o in rows)
    total = convert_from_usd(total_usd, cur)

    # Count-style questions.
    if re.search(r"\b(how many|number of|count|quantity of orders|orders count)\b", low):
        return f"{len(rows)} {kind} orders."

    # Ranking / best-selling product questions.
    if ("top product" in low or "best-selling" in low or "bestselling" in low or
        "most sold" in low or "which product" in low or "by product" in low):
        groups = {}
        for o in rows:
            p = str(o.get("product", "Unknown"))
            groups[p] = groups.get(p, 0.0) + money_usd(o)
        if not groups:
            return "No matching orders found."
        p, v = max(groups.items(), key=lambda x: x[1])
        return f"{p} has the highest {kind} value: {format_money(convert_from_usd(v, cur), cur)}."

    # Customer-focused questions.
    if "customer" in low or re.search(r"\bC\d+\b", q, re.I):
        groups = {}
        for o in rows:
            c = str(o.get("customer", "Unknown"))
            groups[c] = groups.get(c, 0.0) + money_usd(o)
        if not groups:
            return "No matching customer orders found."
        if re.search(r"\b(top|highest|most|largest|best)\b", low) and not re.search(r"\bC\d+\b", q, re.I):
            c, v = max(groups.items(), key=lambda x: x[1])
            return f"Customer {c} has the highest {kind} value: {format_money(convert_from_usd(v, cur), cur)}."
        if re.search(r"\bC\d+\b", q, re.I):
            c = re.search(r"\bC\d+\b", q, re.I).group(0).upper()
            return f"Customer {c}: {format_money(total, cur)} across {len(rows)} matching {kind} orders."
        return f"{len(groups)} customers; total {kind} value is {format_money(total, cur)}."

    # Product counts or product revenue.
    if "product" in low and ("count" in low or "how many" in low or "number" in low):
        return f"{len(rows)} matching {kind} orders."

    if not rows:
        return "No matching orders found."

    return f"Total {kind} from {len(rows)} orders: {format_money(total, cur)}."


@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "ledger-agent",
        "loaded_orders": len(orders),
        "error": load_error
    }


@app.post("/")
def ask(body: Question):
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="question must not be empty")
    return {"answer": answer_question(q)}


@app.post("/reload")
def reload_data():
    load_data()
    return {"loaded_orders": len(orders), "error": load_error}
