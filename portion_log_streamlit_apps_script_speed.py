from __future__ import annotations

import json
import re
import uuid
from html import escape
from textwrap import dedent
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st


# -----------------------------------------------------------------------------
# App configuration
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Portion-Log Cloud",
    page_icon="🍎",
    layout="wide",
    initial_sidebar_state="expanded",
)

INVENTORY_SHEET = "inventory"
SETTINGS_SHEET = "settings"
LOCAL_INVENTORY_PATH = Path("portion_log_inventory.csv")
LOCAL_SETTINGS_PATH = Path("portion_log_settings.json")

INVENTORY_COLUMNS = [
    "id",
    "fridge",
    "category",
    "name",
    "weight",
    "price",
    "expiry_date",
    "status",
    "created_at",
    "updated_at",
    "handled_at",
    "memo",
    "amount_type",
    "quantity",
    "unit",
]

DEFAULT_SETTINGS = {
    "fridge_list": ["기본 장소"],
    "category_list": ["냉장", "냉동"],
    "default_weight": "",
    "default_quantity": "1",
    "default_count": "1",
    "default_expiry_days": "",
    "user_memo": "배고프다",
}

STATUS_ACTIVE = "보관중"
STATUS_CONSUMED = "소비"
STATUS_WASTED = "폐기"
STATUS_DELETED = "삭제"
ALL_STATUS = [STATUS_ACTIVE, STATUS_CONSUMED, STATUS_WASTED, STATUS_DELETED]

SESSION_DF_KEY = "portion_log_df"
SESSION_SETTINGS_KEY = "portion_log_settings"
SESSION_STORAGE_KEY = "portion_log_storage_mode"


# -----------------------------------------------------------------------------
# Small utilities
# -----------------------------------------------------------------------------
def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_date() -> date:
    return date.today()


def normalize_private_key(value: str) -> str:
    """Streamlit secrets sometimes store newlines as escaped \\n."""
    return value.replace("\\n", "\n")


def parse_list(value: Any, fallback: List[str]) -> List[str]:
    if isinstance(value, list):
        cleaned = [str(x).strip() for x in value if str(x).strip()]
        return cleaned or fallback
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            if isinstance(loaded, list):
                cleaned = [str(x).strip() for x in loaded if str(x).strip()]
                return cleaned or fallback
        except Exception:
            pass
        cleaned = [x.strip() for x in value.splitlines() if x.strip()]
        return cleaned or fallback
    return fallback


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def parse_date(value: Any) -> Optional[date]:
    if value is None or value == "" or pd.isna(value):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return pd.to_datetime(value).date()
    except Exception:
        return None


def date_to_text(value: Optional[date]) -> str:
    return value.isoformat() if value else ""


def make_empty_inventory() -> pd.DataFrame:
    return pd.DataFrame(columns=INVENTORY_COLUMNS)


def clean_inventory_df(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure required columns and consistent types."""
    if df is None or df.empty:
        df = make_empty_inventory()
    df = df.copy()

    for col in INVENTORY_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[INVENTORY_COLUMNS]

    # Clean text fields
    for col in ["id", "fridge", "category", "name", "expiry_date", "status", "created_at", "updated_at", "handled_at", "memo", "amount_type", "unit"]:
        df[col] = df[col].fillna("").astype(str)

    df["weight"] = df["weight"].apply(lambda x: safe_float(x, 0.0))
    df["quantity"] = df["quantity"].apply(lambda x: safe_float(x, 0.0))
    df["price"] = df["price"].apply(lambda x: safe_int(x, 0))

    # Backward compatibility: old rows only had weight. Treat them as gram-based rows.
    df["amount_type"] = df["amount_type"].replace("", "weight")
    df.loc[~df["amount_type"].isin(["weight", "count"]), "amount_type"] = "weight"
    weight_mask = df["amount_type"] == "weight"
    count_mask = df["amount_type"] == "count"
    df.loc[weight_mask & (df["quantity"] <= 0), "quantity"] = df.loc[weight_mask & (df["quantity"] <= 0), "weight"]
    df.loc[weight_mask, "unit"] = "g"
    df.loc[count_mask, "unit"] = "개"
    df.loc[count_mask, "quantity"] = df.loc[count_mask, "quantity"].round().clip(lower=0)

    df["status"] = df["status"].replace("", STATUS_ACTIVE)
    df.loc[~df["status"].isin(ALL_STATUS), "status"] = STATUS_ACTIVE

    # Give missing rows stable IDs.
    missing_id = df["id"].str.strip() == ""
    df.loc[missing_id, "id"] = [str(uuid.uuid4()) for _ in range(int(missing_id.sum()))]

    # Normalize date strings to YYYY-MM-DD when possible.
    df["expiry_date"] = df["expiry_date"].apply(lambda x: date_to_text(parse_date(x)))
    return df


def clean_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    merged = DEFAULT_SETTINGS.copy()
    merged.update(settings or {})
    merged["fridge_list"] = parse_list(merged.get("fridge_list"), DEFAULT_SETTINGS["fridge_list"])
    merged["category_list"] = parse_list(merged.get("category_list"), DEFAULT_SETTINGS["category_list"])
    merged["default_weight"] = str(merged.get("default_weight", ""))
    merged["default_quantity"] = str(merged.get("default_quantity", "1") or "1")
    merged["default_count"] = str(merged.get("default_count", "1") or "1")
    merged["default_expiry_days"] = str(merged.get("default_expiry_days", ""))
    merged["user_memo"] = str(merged.get("user_memo", ""))
    return merged


def settings_to_sheet_rows(settings: Dict[str, Any]) -> List[List[str]]:
    rows = [["key", "value"]]
    for key, value in settings.items():
        if isinstance(value, list):
            value = json.dumps(value, ensure_ascii=False)
        rows.append([key, str(value)])
    return rows


def settings_from_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for row in records:
        key = str(row.get("key", "")).strip()
        if not key:
            continue
        result[key] = row.get("value", "")
    return clean_settings(result)


def display_dday(expiry_text: str) -> Tuple[str, Optional[int]]:
    exp = parse_date(expiry_text)
    if not exp:
        return "기한 없음", None
    delta = (exp - today_date()).days
    if delta < 0:
        return "기한 만료", delta
    if delta == 0:
        return "오늘 만료", delta
    return f"{delta}일 남음", delta


def expiry_badge(expiry_text: str) -> str:
    label, delta = display_dday(expiry_text)
    if delta is None:
        return f"<span class='badge normal'>{label}</span>"
    if delta < 0:
        return f"<span class='badge expired'>{label}</span>"
    if delta <= 3:
        return f"<span class='badge danger'>{label}</span>"
    return f"<span class='badge safe'>{label}</span>"


def compute_risk_score(row: pd.Series) -> float:
    """Simple final-project-friendly risk score: deadline urgency + price weight."""
    _, delta = display_dday(row.get("expiry_date", ""))
    price = safe_int(row.get("price", 0))

    if delta is None:
        urgency = 0
    elif delta < 0:
        urgency = 100
    else:
        urgency = max(0, 40 - delta * 6)

    price_score = min(30, price / 1000)  # every 1,000 KRW adds 1 point, capped at 30
    return round(urgency + price_score, 1)


def format_money(value: Any) -> str:
    return f"{safe_int(value):,}원"


def format_weight(value: Any) -> str:
    return f"{safe_float(value):,.1f}g"


def format_amount(row_or_amount: Any, amount_type: Optional[str] = None, unit: Optional[str] = None) -> str:
    """Display either gram-based amount or count-based amount."""
    if isinstance(row_or_amount, pd.Series):
        row = row_or_amount
        amount_type = str(row.get("amount_type", "weight") or "weight")
        unit = str(row.get("unit", "g") or "g")
        if amount_type == "count":
            return f"{safe_int(row.get('quantity', 0)):,}개"
        return format_weight(row.get("weight", row.get("quantity", 0)))

    if amount_type == "count" or unit == "개":
        return f"{safe_int(row_or_amount):,}개"
    return format_weight(row_or_amount)




def html_escape(value: Any) -> str:
    """Escape user-entered values before placing them inside custom HTML cards."""
    if value is None:
        return ""
    return escape(str(value), quote=True)


def split_integer_amount(total: int, parts: int) -> List[int]:
    """Split an integer count into near-even integer portions, e.g. 10 eggs into 3 -> [4, 3, 3]."""
    total = max(0, int(total))
    parts = max(1, int(parts))
    base, remainder = divmod(total, parts)
    return [base + (1 if i < remainder else 0) for i in range(parts)]


def split_price_by_amount(total_price: int, amounts: List[float]) -> List[int]:
    """Split price proportionally while preserving the exact total price."""
    total_price = int(total_price)
    if not amounts:
        return []
    total_amount = sum(float(x) for x in amounts)
    if total_amount <= 0:
        base, rem = divmod(total_price, len(amounts))
        return [base + (1 if i < rem else 0) for i in range(len(amounts))]
    raw = [total_price * (float(x) / total_amount) for x in amounts]
    prices = [int(x) for x in raw]
    diff = total_price - sum(prices)
    for i in range(abs(diff)):
        idx = i % len(prices)
        prices[idx] += 1 if diff > 0 else -1
    return prices


# -----------------------------------------------------------------------------
# Storage: Google Apps Script Web App first, local CSV fallback second
# -----------------------------------------------------------------------------
def get_apps_script_config() -> Tuple[str, str]:
    """Read Apps Script Web App URL and API key from Streamlit secrets."""
    try:
        if "apps_script" in st.secrets:
            web_app_url = str(st.secrets["apps_script"].get("web_app_url", "")).strip()
            api_key = str(st.secrets["apps_script"].get("api_key", "")).strip()
            return web_app_url, api_key
        # Also support flat keys for quick local testing.
        web_app_url = str(st.secrets.get("web_app_url", "")).strip()
        api_key = str(st.secrets.get("api_key", "")).strip()
        return web_app_url, api_key
    except Exception:
        return "", ""


def use_apps_script() -> bool:
    web_app_url, api_key = get_apps_script_config()
    return bool(web_app_url and api_key)


def call_apps_script(action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Call the Google Apps Script web app.

    Apps Script is used as a lightweight API layer in front of Google Sheets.
    The API key is not a Google Cloud key. It is just a shared password between
    this Streamlit app and Code.gs.
    """
    web_app_url, api_key = get_apps_script_config()
    if not web_app_url or not api_key:
        raise RuntimeError("Apps Script web_app_url 또는 api_key가 설정되지 않았습니다.")

    body: Dict[str, Any] = {"action": action, "key": api_key}
    if payload:
        body.update(payload)

    try:
        response = requests.post(web_app_url, json=body, timeout=15)
    except requests.RequestException as exc:
        raise RuntimeError(f"Apps Script 요청 실패: {exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(f"Apps Script HTTP 오류 {response.status_code}: {response.text[:500]}")

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "Apps Script 응답이 JSON이 아닙니다. Web App 배포 권한이 '모든 사용자'인지, URL이 /exec로 끝나는지 확인하세요.\n"
            f"응답 일부: {response.text[:500]}"
        ) from exc

    if not data.get("ok", False):
        raise RuntimeError(str(data.get("error", "알 수 없는 Apps Script 오류")))
    return data


def load_from_apps_script() -> Tuple[pd.DataFrame, Dict[str, Any]]:
    data = call_apps_script("read")
    df = clean_inventory_df(pd.DataFrame(data.get("inventory", [])))
    settings = clean_settings(data.get("settings", {}))
    return df, settings


@st.cache_data(ttl=60, show_spinner=False)
def load_from_apps_script_cached(web_app_url: str, api_key: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    # web_app_url/api_key are arguments only so Streamlit knows when to invalidate this cache.
    data = call_apps_script("read")
    df = clean_inventory_df(pd.DataFrame(data.get("inventory", [])))
    settings = clean_settings(data.get("settings", {}))
    return df, settings


def save_to_apps_script(df: pd.DataFrame, settings: Dict[str, Any]) -> None:
    df = clean_inventory_df(df)
    settings = clean_settings(settings)
    # Convert DataFrame rows to plain JSON-safe dictionaries.
    records = json.loads(df.to_json(orient="records", force_ascii=False))
    call_apps_script("save_all", {"inventory": records, "settings": settings})


def load_from_local() -> Tuple[pd.DataFrame, Dict[str, Any]]:
    if LOCAL_INVENTORY_PATH.exists():
        df = pd.read_csv(LOCAL_INVENTORY_PATH, dtype=str)
    else:
        df = make_empty_inventory()

    if LOCAL_SETTINGS_PATH.exists():
        try:
            settings = json.loads(LOCAL_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            settings = DEFAULT_SETTINGS.copy()
    else:
        settings = DEFAULT_SETTINGS.copy()

    return clean_inventory_df(df), clean_settings(settings)


def save_to_local(df: pd.DataFrame, settings: Dict[str, Any]) -> None:
    clean_inventory_df(df).to_csv(LOCAL_INVENTORY_PATH, index=False, encoding="utf-8-sig")
    LOCAL_SETTINGS_PATH.write_text(json.dumps(clean_settings(settings), ensure_ascii=False, indent=2), encoding="utf-8")


def set_session_data(df: pd.DataFrame, settings: Dict[str, Any], storage_mode: str) -> None:
    st.session_state[SESSION_DF_KEY] = clean_inventory_df(df)
    st.session_state[SESSION_SETTINGS_KEY] = clean_settings(settings)
    st.session_state[SESSION_STORAGE_KEY] = storage_mode


def clear_session_data() -> None:
    for key in [SESSION_DF_KEY, SESSION_SETTINGS_KEY, SESSION_STORAGE_KEY]:
        st.session_state.pop(key, None)
    st.cache_data.clear()


def load_data(force_reload: bool = False) -> Tuple[pd.DataFrame, Dict[str, Any], str]:
    # Speed improvement: keep the current working data in session_state.
    # Google Sheets is read once when the app starts, after a manual refresh, or when cache expires.
    if (
        not force_reload
        and SESSION_DF_KEY in st.session_state
        and SESSION_SETTINGS_KEY in st.session_state
        and SESSION_STORAGE_KEY in st.session_state
    ):
        return (
            clean_inventory_df(st.session_state[SESSION_DF_KEY]),
            clean_settings(st.session_state[SESSION_SETTINGS_KEY]),
            str(st.session_state[SESSION_STORAGE_KEY]),
        )

    if use_apps_script():
        try:
            web_app_url, api_key = get_apps_script_config()
            df, settings = load_from_apps_script_cached(web_app_url, api_key)
            set_session_data(df, settings, "Google Apps Script + Sheets")
            return df, settings, "Google Apps Script + Sheets"
        except Exception as exc:
            st.warning(f"Google Apps Script 연결 실패: {exc}\n로컬 CSV 모드로 임시 실행합니다.")
    df, settings = load_from_local()
    set_session_data(df, settings, "Local CSV")
    return df, settings, "Local CSV"


def save_data(df: pd.DataFrame, settings: Dict[str, Any], storage_mode: str) -> None:
    # Full save fallback. Incremental helpers below are used for common edits.
    if storage_mode == "Google Apps Script + Sheets" and use_apps_script():
        save_to_apps_script(df, settings)
    else:
        save_to_local(df, settings)
    set_session_data(df, settings, storage_mode)


def records_for_apps_script(df_or_rows: Any) -> List[Dict[str, Any]]:
    if isinstance(df_or_rows, pd.DataFrame):
        df = clean_inventory_df(df_or_rows)
        return json.loads(df.to_json(orient="records", force_ascii=False))
    return json.loads(pd.DataFrame(df_or_rows).to_json(orient="records", force_ascii=False))


def persist_append_items(df: pd.DataFrame, settings: Dict[str, Any], storage_mode: str, rows: List[Dict[str, Any]], settings_changed: bool = False) -> pd.DataFrame:
    new_df = append_items(df, rows)
    if storage_mode == "Google Apps Script + Sheets" and use_apps_script():
        payload: Dict[str, Any] = {"inventory": records_for_apps_script(rows)}
        if settings_changed:
            payload["settings"] = clean_settings(settings)
        call_apps_script("append_items", payload)
    else:
        save_to_local(new_df, settings)
    set_session_data(new_df, settings, storage_mode)
    return new_df


def persist_item_updates(df: pd.DataFrame, settings: Dict[str, Any], storage_mode: str, item_id: str, updates: Dict[str, Any]) -> pd.DataFrame:
    new_df = update_item(df, item_id, updates)
    if storage_mode == "Google Apps Script + Sheets" and use_apps_script():
        call_apps_script("update_item", {"id": item_id, "updates": updates})
    else:
        save_to_local(new_df, settings)
    set_session_data(new_df, settings, storage_mode)
    return new_df


def persist_hard_delete(df: pd.DataFrame, settings: Dict[str, Any], storage_mode: str, item_id: str) -> pd.DataFrame:
    new_df = hard_delete_item(df, item_id)
    if storage_mode == "Google Apps Script + Sheets" and use_apps_script():
        call_apps_script("hard_delete_item", {"id": item_id})
    else:
        save_to_local(new_df, settings)
    set_session_data(new_df, settings, storage_mode)
    return new_df


def persist_settings(df: pd.DataFrame, settings: Dict[str, Any], storage_mode: str) -> None:
    settings = clean_settings(settings)
    if storage_mode == "Google Apps Script + Sheets" and use_apps_script():
        call_apps_script("save_settings", {"settings": settings})
    else:
        save_to_local(df, settings)
    set_session_data(df, settings, storage_mode)


# -----------------------------------------------------------------------------
# Data mutation helpers
# -----------------------------------------------------------------------------
def append_items(df: pd.DataFrame, items: List[Dict[str, Any]]) -> pd.DataFrame:
    if not items:
        return clean_inventory_df(df)
    new_df = pd.DataFrame(items)
    return clean_inventory_df(pd.concat([df, new_df], ignore_index=True))


def update_item(df: pd.DataFrame, item_id: str, updates: Dict[str, Any]) -> pd.DataFrame:
    df = clean_inventory_df(df)
    mask = df["id"] == item_id
    if not mask.any():
        return df
    for key, value in updates.items():
        if key in df.columns:
            df.loc[mask, key] = value
    df.loc[mask, "updated_at"] = now_text()
    return clean_inventory_df(df)


def status_updates(status: str) -> Dict[str, Any]:
    handled = now_text() if status in [STATUS_CONSUMED, STATUS_WASTED, STATUS_DELETED] else ""
    return {
        "status": status,
        "updated_at": now_text(),
        "handled_at": handled,
    }


def set_item_status(df: pd.DataFrame, item_id: str, status: str) -> pd.DataFrame:
    return update_item(df, item_id, status_updates(status))


def hard_delete_item(df: pd.DataFrame, item_id: str) -> pd.DataFrame:
    df = clean_inventory_df(df)
    return df[df["id"] != item_id].reset_index(drop=True)


# -----------------------------------------------------------------------------
# UI helpers
# -----------------------------------------------------------------------------
def inject_css() -> None:
    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
        .metric-card {
            border: 1px solid rgba(128, 128, 128, 0.25);
            border-radius: 16px;
            padding: 1rem;
            background: var(--secondary-background-color);
            color: var(--text-color);
        }
        .food-card {
            border: 1px solid rgba(128, 128, 128, 0.28);
            border-radius: 16px;
            padding: 1rem 1rem 0.75rem 1rem;
            margin-bottom: 0.75rem;
            background: var(--secondary-background-color);
            color: var(--text-color);
            box-shadow: 0 1px 8px rgba(0,0,0,0.14);
        }
        .card-title { color: var(--text-color); font-size: 1.08rem; font-weight: 700; margin-bottom: 0.25rem; }
        .card-meta { color: var(--text-color); opacity: 0.86; font-size: 0.92rem; line-height: 1.7; }
        .badge {
            display: inline-block;
            border-radius: 999px;
            padding: 0.18rem 0.55rem;
            font-size: 0.8rem;
            font-weight: 700;
        }
        .safe { background: #e8f5e9; color: #1b5e20; }
        .danger { background: #ffebee; color: #b71c1c; }
        .expired { background: #e3f2fd; color: #0d47a1; text-decoration: line-through; }
        .normal { background: #eeeeee; color: #424242; }
        .small-note { color: #666; font-size: 0.88rem; }
        @media (max-width: 760px) {
            .block-container { padding-left: 0.8rem; padding-right: 0.8rem; }
            div[data-testid="stHorizontalBlock"] { gap: 0.4rem; }
            .food-card { padding: 0.85rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def show_storage_status(storage_mode: str) -> None:
    if storage_mode == "Google Apps Script + Sheets":
        st.sidebar.success("저장소: Google Apps Script + Google Sheets")
    else:
        st.sidebar.info("저장소: Local CSV")
        st.sidebar.caption("Apps Script secrets를 설정하면 Google Sheets 클라우드 저장소로 전환됩니다.")


def active_df(df: pd.DataFrame) -> pd.DataFrame:
    return clean_inventory_df(df)[lambda x: x["status"] == STATUS_ACTIVE].copy()


def show_alerts(df: pd.DataFrame) -> None:
    current = active_df(df)
    if current.empty:
        return
    current["dday_num"] = current["expiry_date"].apply(lambda x: display_dday(x)[1])
    expired = current[current["dday_num"].notna() & (current["dday_num"] < 0)]
    danger = current[current["dday_num"].notna() & (current["dday_num"].between(0, 3))]

    if not expired.empty:
        names = ", ".join(expired["name"].head(5).tolist())
        suffix = "..." if len(expired) > 5 else ""
        st.error(f"기한이 지난 품목이 {len(expired)}개 있습니다: {names}{suffix}")
    if not danger.empty:
        names = ", ".join(danger["name"].head(5).tolist())
        suffix = "..." if len(danger) > 5 else ""
        st.warning(f"유통기한 3일 이내 품목이 {len(danger)}개 있습니다: {names}{suffix}")


def show_summary_metrics(df: pd.DataFrame) -> None:
    df = clean_inventory_df(df)
    current = df[df["status"] == STATUS_ACTIVE]
    consumed = df[df["status"] == STATUS_CONSUMED]
    wasted = df[df["status"] == STATUS_WASTED]

    inventory_value = int(current["price"].sum()) if not current.empty else 0
    consumed_value = int(consumed["price"].sum()) if not consumed.empty else 0
    wasted_value = int(wasted["price"].sum()) if not wasted.empty else 0
    total_handled = consumed_value + wasted_value
    waste_rate = (wasted_value / total_handled * 100) if total_handled > 0 else 0.0

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("🏠 현재 재고 가치", format_money(inventory_value), f"{len(current)}개 보관중")
    col2.metric("✅ 소비 금액", format_money(consumed_value), f"{len(consumed)}개")
    col3.metric("🗑️ 폐기 금액", format_money(wasted_value), f"{len(wasted)}개")
    col4.metric("📉 폐기율", f"{waste_rate:.1f}%", "소비+폐기 기준")

def base_item_name(name: Any) -> str:
    """Remove automatic portion suffix such as '계란 (1/5)' for grouped recommendations."""
    cleaned = str(name or "").strip()
    return re.sub(r"\s+\(\d+/\d+\)$", "", cleaned).strip() or cleaned


def summarize_unique(values: pd.Series, max_items: int = 2) -> str:
    unique_values = [str(v).strip() for v in values.tolist() if str(v).strip()]
    unique_values = list(dict.fromkeys(unique_values))
    if not unique_values:
        return "-"
    if len(unique_values) <= max_items:
        return ", ".join(unique_values)
    return ", ".join(unique_values[:max_items]) + f" 외 {len(unique_values) - max_items}개"


def summarize_group_amount(group: pd.DataFrame) -> str:
    amount_types = set(str(x or "weight") for x in group.get("amount_type", pd.Series(dtype=str)).tolist())
    if amount_types == {"count"}:
        return f"{int(group['quantity'].apply(lambda x: safe_float(x, 0)).sum()):,}개"
    if amount_types == {"weight"}:
        return format_weight(group['weight'].apply(lambda x: safe_float(x, 0)).sum())
    return f"{len(group):,}개 항목"


def show_recommendations(df: pd.DataFrame) -> None:
    current = active_df(df)
    if current.empty:
        st.info("아직 보관중인 식재료가 없습니다. 먼저 식재료를 등록해 주세요.")
        return

    current = current.copy()
    current["risk_score"] = current.apply(compute_risk_score, axis=1)
    current["base_name"] = current["name"].apply(base_item_name)
    current["expiry_sort"] = current["expiry_date"].apply(lambda x: parse_date(x) or date.max)

    grouped_rows: List[Dict[str, Any]] = []
    for base_name, group in current.groupby("base_name", sort=False):
        group = group.sort_values(["expiry_sort", "risk_score", "price"], ascending=[True, False, False])
        earliest = group.iloc[0]
        grouped_rows.append(
            {
                "base_name": base_name,
                "item_count": len(group),
                "fridge": summarize_unique(group["fridge"]),
                "category": summarize_unique(group["category"]),
                "amount": summarize_group_amount(group),
                "price": int(group["price"].apply(lambda x: safe_int(x, 0)).sum()),
                "expiry_date": earliest.get("expiry_date", ""),
                "risk_score": round(float(group["risk_score"].max()), 1),
                "expiry_sort": earliest.get("expiry_sort", date.max),
            }
        )

    grouped = pd.DataFrame(grouped_rows)
    grouped = grouped.sort_values(["risk_score", "price", "expiry_sort"], ascending=[False, False, True]).head(5)

    st.subheader("🔥 오늘 먼저 소비할 재료 TOP 5")
    for rank, (_, row) in enumerate(grouped.iterrows(), start=1):
        item_count = safe_int(row.get("item_count", 1), 1)
        title_suffix = f" ({item_count}개 소분)" if item_count > 1 else ""
        dday_label, _ = display_dday(row.get("expiry_date", ""))
        st.markdown(
            dedent(
                f"""
                <div class='food-card'>
                    <div class='card-title'>{rank}. {html_escape(row['base_name'])}{title_suffix} {expiry_badge(row['expiry_date'])}</div>
                    <div class='card-meta'>
                        {html_escape(row['fridge'])} · {html_escape(row['category'])} · {html_escape(row['amount'])} · {html_escape(format_money(row['price']))}<br>
                        유통기한: {html_escape(row['expiry_date'] or 'N/A')} · {html_escape(dday_label)} · 위험점수: {html_escape(row['risk_score'])}
                    </div>
                </div>
                """
            ).strip(),
            unsafe_allow_html=True,
        )


def show_simple_charts(df: pd.DataFrame) -> None:
    df = clean_inventory_df(df)
    if df.empty:
        return
    st.subheader("📊 통계")
    status_sum = df.groupby("status", as_index=True)["price"].sum().reindex(ALL_STATUS).fillna(0)
    st.bar_chart(status_sum)

    current = active_df(df)
    if not current.empty:
        cat_sum = current.groupby("category", as_index=True)["price"].sum().sort_values(ascending=False)
        st.caption("보관 유형별 현재 재고 가치")
        st.bar_chart(cat_sum)


def add_manual_value(options: List[str], manual: str) -> List[str]:
    manual = manual.strip()
    if manual and manual not in options:
        return options + [manual]
    return options


# -----------------------------------------------------------------------------
# Pages
# -----------------------------------------------------------------------------
def page_dashboard_and_register(df: pd.DataFrame, settings: Dict[str, Any], storage_mode: str) -> None:
    st.title("🍎 Portion-Log Cloud")
    st.caption("Google Apps Script를 거쳐 Google Sheets에 저장하는 모바일 접근형 Streamlit 버전입니다.")

    show_alerts(df)
    show_summary_metrics(df)

    left, right = st.columns([1.05, 0.95])

    with left:
        st.subheader("➕ 식재료 등록")
        fridge_list = settings["fridge_list"]
        category_list = settings["category_list"]

        # st.form은 제출 버튼을 누르기 전까지 radio/selectbox 변경이 화면에 즉시 반영되지 않습니다.
        # 모바일 입력 편의성을 위해 일반 container + button 구조로 변경했습니다.
        with st.container():
            c1, c2 = st.columns(2)
            with c1:
                fridge = st.selectbox("보관 장소", fridge_list + ["직접 입력"], index=0)
                fridge_manual = ""
                if fridge == "직접 입력":
                    fridge_manual = st.text_input("새 보관 장소")
            with c2:
                category = st.selectbox("보관 유형", category_list + ["직접 입력"], index=0)
                category_manual = ""
                if category == "직접 입력":
                    category_manual = st.text_input("새 보관 유형")

            name = st.text_input("재고명", placeholder="예: 닭가슴살, 계란, 양파")
            amount_mode = st.radio("수량 입력 방식", ["무게(g)로 입력", "개수(개)로 입력"], horizontal=True)
            st.caption("예: 닭가슴살은 무게(g), 계란·김·음료는 개수(개)로 입력하면 편합니다.")
            c3, c4, c5 = st.columns(3)
            with c3:
                if amount_mode == "무게(g)로 입력":
                    default_weight = safe_float(settings.get("default_weight", ""), 0.0)
                    total_weight = st.number_input("총무게(g)", min_value=0.0, value=default_weight, step=10.0)
                    total_quantity = 0
                else:
                    default_quantity = max(1, safe_int(settings.get("default_quantity", "1"), 1))
                    total_quantity = st.number_input("총개수(개)", min_value=1, value=default_quantity, step=1)
                    total_weight = 0.0
            with c4:
                total_price = st.number_input("총가격(원)", min_value=0, value=0, step=100)
            with c5:
                default_count = max(1, safe_int(settings.get("default_count", "1"), 1))
                count = st.number_input("나눌 묶음 수", min_value=1, value=default_count, step=1)
            if amount_mode == "개수(개)로 입력":
                st.caption("예: 계란 10개를 한 줄로 저장하려면 총개수 10, 나눌 묶음 수 1로 입력하세요. 10개를 각각 나누려면 나눌 묶음 수 10으로 입력하세요.")

            expiry_mode = st.radio("유통기한 입력 방식", ["며칠 뒤 만료", "날짜 선택", "없음"], horizontal=True)
            expiry_date: Optional[date] = None
            if expiry_mode == "며칠 뒤 만료":
                default_days = safe_int(settings.get("default_expiry_days", ""), 0)
                days = st.number_input("기한(일)", min_value=0, value=default_days, step=1)
                expiry_date = today_date() + timedelta(days=int(days))
            elif expiry_mode == "날짜 선택":
                expiry_date = st.date_input("만료예정일", value=today_date())

            detailed = False
            portion_amounts: List[float] = []
            if count > 1:
                detailed_label = "상세 소분: 조각별 무게를 직접 입력" if amount_mode == "무게(g)로 입력" else "상세 소분: 묶음별 개수를 직접 입력"
                detailed = st.checkbox(detailed_label)
                if detailed:
                    cols = st.columns(2)
                    if amount_mode == "무게(g)로 입력":
                        st.caption("총무게보다 적게 입력하면 손질 로스를 반영한 것으로 처리합니다.")
                        equal_weight = total_weight / count if count else 0
                        for i in range(int(count)):
                            with cols[i % 2]:
                                portion_amounts.append(
                                    st.number_input(
                                        f"{i + 1}번 소분 무게(g)",
                                        min_value=0.0,
                                        value=float(equal_weight),
                                        step=10.0,
                                        key=f"detail_weight_{i}",
                                    )
                                )
                        if total_weight > 0 and sum(portion_amounts) > total_weight:
                            st.warning("상세 소분 무게 합계가 총무게보다 큽니다. 입력값을 확인해 주세요.")
                    else:
                        st.caption("총개수와 다르게 입력하면 입력한 묶음별 개수만 저장됩니다.")
                        default_parts = split_integer_amount(int(total_quantity), int(count))
                        for i in range(int(count)):
                            with cols[i % 2]:
                                portion_amounts.append(
                                    float(
                                        st.number_input(
                                            f"{i + 1}번 묶음 개수(개)",
                                            min_value=0,
                                            value=int(default_parts[i]),
                                            step=1,
                                            key=f"detail_quantity_{i}",
                                        )
                                    )
                                )
                        if sum(portion_amounts) > int(total_quantity):
                            st.warning("상세 소분 개수 합계가 총개수보다 큽니다. 입력값을 확인해 주세요.")

            memo = st.text_input("메모", placeholder="선택 입력")
            submitted = st.button("식재료 등록하기", use_container_width=True)

        if submitted:
            fridge_final = fridge_manual.strip() if fridge == "직접 입력" else fridge
            category_final = category_manual.strip() if category == "직접 입력" else category
            if not name.strip():
                st.error("재고명을 입력하세요.")
            elif not fridge_final or not category_final:
                st.error("보관 장소와 보관 유형을 입력하세요.")
            else:
                if fridge_final not in settings["fridge_list"]:
                    settings["fridge_list"].append(fridge_final)
                if category_final not in settings["category_list"]:
                    settings["category_list"].append(category_final)

                created = now_text()
                rows: List[Dict[str, Any]] = []
                count_int = int(count)

                if amount_mode == "개수(개)로 입력" and not detailed and count_int > int(total_quantity):
                    st.error("개수 입력 방식에서는 나눌 묶음 수가 총개수보다 클 수 없습니다.")
                    st.stop()

                if detailed and count_int > 1:
                    source_amounts = portion_amounts
                elif amount_mode == "개수(개)로 입력":
                    source_amounts = [float(x) for x in split_integer_amount(int(total_quantity), count_int)]
                else:
                    each_weight = total_weight / count_int if count_int else 0
                    source_amounts = [each_weight] * count_int

                price_source_total = sum(source_amounts)
                if price_source_total <= 0:
                    price_source_total = float(total_quantity if amount_mode == "개수(개)로 입력" else total_weight)
                prices = split_price_by_amount(int(total_price), source_amounts)

                for i, amount in enumerate(source_amounts, start=1):
                    item_name = f"{name.strip()} ({i}/{count_int})" if count_int > 1 else name.strip()
                    if amount_mode == "개수(개)로 입력":
                        amount_type = "count"
                        quantity = int(amount)
                        unit = "개"
                        weight = 0.0
                    else:
                        amount_type = "weight"
                        quantity = float(amount)
                        unit = "g"
                        weight = float(amount)
                    rows.append(
                        {
                            "id": str(uuid.uuid4()),
                            "fridge": fridge_final,
                            "category": category_final,
                            "name": item_name,
                            "weight": float(weight),
                            "price": int(prices[i - 1]) if i - 1 < len(prices) else 0,
                            "expiry_date": date_to_text(expiry_date),
                            "status": STATUS_ACTIVE,
                            "created_at": created,
                            "updated_at": created,
                            "handled_at": "",
                            "memo": memo,
                            "amount_type": amount_type,
                            "quantity": quantity,
                            "unit": unit,
                        }
                    )
                persist_append_items(df, settings, storage_mode, rows, settings_changed=True)
                st.success(f"{len(rows)}개 식재료를 등록했습니다.")
                st.rerun()

    with right:
        show_recommendations(df)

    show_simple_charts(df)


def build_filtered_inventory(df: pd.DataFrame, settings: Dict[str, Any]) -> pd.DataFrame:
    df = clean_inventory_df(df)
    st.subheader("🔍 검색·필터")
    c1, c2, c3, c4 = st.columns([1.2, 1, 1, 1])
    with c1:
        keyword = st.text_input("품목 검색", placeholder="예: 계란")
    with c2:
        fridge_filter = st.selectbox("보관 장소", ["ALL"] + settings["fridge_list"])
    with c3:
        category_filter = st.selectbox("보관 유형", ["ALL"] + settings["category_list"])
    with c4:
        status_filter = st.selectbox("상태", ["ALL"] + ALL_STATUS, index=1)

    sort_col = st.selectbox("정렬 기준", ["유통기한 빠른순", "가격 높은순", "수량 높은순", "최근 등록순"])

    filtered = df.copy()
    if keyword.strip():
        filtered = filtered[filtered["name"].str.contains(keyword.strip(), case=False, na=False)]
    if fridge_filter != "ALL":
        filtered = filtered[filtered["fridge"] == fridge_filter]
    if category_filter != "ALL":
        filtered = filtered[filtered["category"] == category_filter]
    if status_filter != "ALL":
        filtered = filtered[filtered["status"] == status_filter]

    filtered["expiry_sort"] = filtered["expiry_date"].apply(lambda x: parse_date(x) or date.max)
    if sort_col == "유통기한 빠른순":
        filtered = filtered.sort_values(["expiry_sort", "name"], ascending=[True, True])
    elif sort_col == "가격 높은순":
        filtered = filtered.sort_values("price", ascending=False)
    elif sort_col == "수량 높은순":
        filtered["amount_sort"] = filtered.apply(lambda r: safe_float(r.get("quantity", r.get("weight", 0))), axis=1)
        filtered = filtered.sort_values("amount_sort", ascending=False)
    else:
        filtered = filtered.sort_values("created_at", ascending=False)

    return filtered.drop(columns=["expiry_sort", "amount_sort"], errors="ignore")


def render_item_card(row: pd.Series, df: pd.DataFrame, settings: Dict[str, Any], storage_mode: str) -> None:
    item_id = str(row["id"])
    dday_label, _ = display_dday(row["expiry_date"])

    memo_text = str(row.get("memo", "")).strip()
    memo_html = f"<br>메모: {html_escape(memo_text)}" if memo_text else ""

    st.markdown(
        dedent(
            f"""
            <div class='food-card'>
                <div class='card-title'>{html_escape(row['name'])} {expiry_badge(row['expiry_date'])}</div>
                <div class='card-meta'>
                    {html_escape(row['fridge'])} · {html_escape(row['category'])} · 상태: {html_escape(row['status'])}<br>
                    수량: {html_escape(format_amount(row))} · 가격: {html_escape(format_money(row['price']))}<br>
                    만료예정일: {html_escape(row['expiry_date'] or 'N/A')} · 남은기한: {html_escape(dday_label)}{memo_html}
                </div>
            </div>
            """
        ).strip(),
        unsafe_allow_html=True,
    )

    pending_key = f"pending_action_{item_id}"
    pending_action = st.session_state.get(pending_key, "")

    action_meta = {
        "consume": ("소비 처리", STATUS_CONSUMED, "success", "소비 처리했습니다."),
        "waste": ("폐기 처리", STATUS_WASTED, "warning", "폐기 처리했습니다."),
        "delete": ("삭제(통계 제외)", STATUS_DELETED, "info", "삭제 상태로 변경했습니다."),
        "hard_delete": ("영구 삭제", "", "info", "행을 영구 삭제했습니다."),
    }

    if pending_action in action_meta:
        action_label, target_status, message_type, done_message = action_meta[pending_action]
        st.warning(f"'{row['name']}' 항목을 {action_label}할까요?")
        c_confirm, c_cancel = st.columns(2)
        if c_confirm.button("확인", key=f"confirm_{pending_action}_{item_id}", use_container_width=True):
            if pending_action == "hard_delete":
                df = persist_hard_delete(df, settings, storage_mode, item_id)
            else:
                df = persist_item_updates(df, settings, storage_mode, item_id, status_updates(target_status))
            st.session_state.pop(pending_key, None)
            if message_type == "success":
                st.success(done_message)
            elif message_type == "warning":
                st.warning(done_message)
            else:
                st.info(done_message)
            st.rerun()
        if c_cancel.button("취소", key=f"cancel_{pending_action}_{item_id}", use_container_width=True):
            st.session_state.pop(pending_key, None)
            st.rerun()
    else:
        b1, b2, b3, b4 = st.columns(4)
        if row["status"] == STATUS_ACTIVE:
            if b1.button("소비 처리", key=f"consume_{item_id}", use_container_width=True):
                st.session_state[pending_key] = "consume"
                st.rerun()
            if b2.button("폐기 처리", key=f"waste_{item_id}", use_container_width=True):
                st.session_state[pending_key] = "waste"
                st.rerun()
            if b3.button("삭제(통계 제외)", key=f"delete_{item_id}", use_container_width=True):
                st.session_state[pending_key] = "delete"
                st.rerun()
        else:
            if b1.button("복구", key=f"restore_{item_id}", use_container_width=True):
                df = persist_item_updates(df, settings, storage_mode, item_id, status_updates(STATUS_ACTIVE))
                st.success("보관중 상태로 복구했습니다.")
                st.rerun()
            if b2.button("영구 삭제", key=f"hard_delete_{item_id}", use_container_width=True):
                st.session_state[pending_key] = "hard_delete"
                st.rerun()

    with st.expander("항목 수정하기"):
        with st.form(f"edit_form_{item_id}"):
            c1, c2 = st.columns(2)
            with c1:
                new_name = st.text_input("재고명", value=str(row["name"]), key=f"edit_name_{item_id}")
                fridge_options = add_manual_value(settings["fridge_list"], str(row["fridge"]))
                new_fridge = st.selectbox(
                    "보관 장소",
                    fridge_options,
                    index=fridge_options.index(str(row["fridge"])) if str(row["fridge"]) in fridge_options else 0,
                    key=f"edit_fridge_{item_id}",
                )
                current_amount_type = str(row.get("amount_type", "weight") or "weight")
                edit_amount_mode = st.radio(
                    "수량 방식",
                    ["무게(g)", "개수(개)"],
                    index=1 if current_amount_type == "count" else 0,
                    horizontal=True,
                    key=f"edit_amount_mode_{item_id}",
                )
                if edit_amount_mode == "개수(개)":
                    new_amount = st.number_input("개수(개)", min_value=0, value=safe_int(row.get("quantity", 0)), step=1, key=f"edit_quantity_{item_id}")
                else:
                    base_weight = safe_float(row.get("weight", row.get("quantity", 0)))
                    new_amount = st.number_input("무게(g)", min_value=0.0, value=base_weight, step=10.0, key=f"edit_weight_{item_id}")
            with c2:
                category_options = add_manual_value(settings["category_list"], str(row["category"]))
                new_category = st.selectbox(
                    "보관 유형",
                    category_options,
                    index=category_options.index(str(row["category"])) if str(row["category"]) in category_options else 0,
                    key=f"edit_category_{item_id}",
                )
                new_price = st.number_input("가격(원)", min_value=0, value=safe_int(row["price"]), step=100, key=f"edit_price_{item_id}")
                current_expiry = parse_date(row["expiry_date"]) or today_date()
                use_expiry = st.checkbox("유통기한 있음", value=bool(row["expiry_date"]), key=f"edit_use_expiry_{item_id}")
                new_expiry = st.date_input("만료예정일", value=current_expiry, key=f"edit_expiry_{item_id}") if use_expiry else None
            new_status = st.selectbox(
                "상태",
                ALL_STATUS,
                index=ALL_STATUS.index(row["status"]) if row["status"] in ALL_STATUS else 0,
                key=f"edit_status_{item_id}",
            )
            new_memo = st.text_input("메모", value=str(row.get("memo", "")), key=f"edit_memo_{item_id}")
            saved = st.form_submit_button("수정 저장", use_container_width=True)

        if saved:
            if not new_name.strip():
                st.error("재고명은 비울 수 없습니다.")
            else:
                updates = {
                    "name": new_name.strip(),
                    "fridge": new_fridge,
                    "category": new_category,
                    "weight": 0.0 if edit_amount_mode == "개수(개)" else float(new_amount),
                    "price": int(new_price),
                    "amount_type": "count" if edit_amount_mode == "개수(개)" else "weight",
                    "quantity": int(new_amount) if edit_amount_mode == "개수(개)" else float(new_amount),
                    "unit": "개" if edit_amount_mode == "개수(개)" else "g",
                    "expiry_date": date_to_text(new_expiry),
                    "status": new_status,
                    "memo": new_memo,
                }
                if new_status in [STATUS_CONSUMED, STATUS_WASTED, STATUS_DELETED] and not str(row.get("handled_at", "")).strip():
                    updates["handled_at"] = now_text()
                if new_status == STATUS_ACTIVE:
                    updates["handled_at"] = ""
                df = persist_item_updates(df, settings, storage_mode, item_id, updates)
                st.success("수정했습니다.")
                st.rerun()


def page_inventory(df: pd.DataFrame, settings: Dict[str, Any], storage_mode: str) -> None:
    st.title("📦 재고 관리")
    filtered = build_filtered_inventory(df, settings)

    st.caption(f"총 {len(filtered)}개 항목")
    if filtered.empty:
        st.info("조건에 맞는 식재료가 없습니다.")
        return

    view_mode = st.radio("보기 방식", ["모바일 카드", "표"], horizontal=True)
    if view_mode == "표":
        table = filtered.copy()
        table["dday"] = table["expiry_date"].apply(lambda x: display_dday(x)[0])
        table["amount"] = table.apply(format_amount, axis=1)
        table["price"] = table["price"].apply(lambda x: f"{safe_int(x):,}")
        table = table.rename(columns={"amount": "수량", "fridge": "보관 장소", "category": "보관 유형", "name": "품목", "price": "가격", "expiry_date": "만료예정일", "dday": "남은기한", "status": "상태", "memo": "메모"})
        st.dataframe(
            table[["보관 장소", "보관 유형", "품목", "수량", "가격", "만료예정일", "남은기한", "상태", "메모"]],
            use_container_width=True,
            hide_index=True,
        )
        st.caption("수정·소비 처리·폐기 처리·삭제는 모바일 카드 보기에서 할 수 있습니다.")
    else:
        for _, row in filtered.iterrows():
            render_item_card(row, df, settings, storage_mode)


def page_settings(df: pd.DataFrame, settings: Dict[str, Any], storage_mode: str) -> None:
    st.title("⚙️ 설정 / 메모")

    with st.form("settings_form"):
        st.subheader("보관 장소·유형 관리")
        st.caption("한 줄에 하나씩 입력하세요. 예: 기본 장소, 자취방 냉장고, 본가 냉동실")
        fridge_text = st.text_area("보관 장소 목록", value="\n".join(settings["fridge_list"]), height=120)
        category_text = st.text_area("보관 유형 목록", value="\n".join(settings["category_list"]), height=100)

        st.subheader("등록 기본값")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            default_weight = st.text_input("총무게(g) 기본값", value=settings.get("default_weight", ""))
        with c2:
            default_quantity = st.text_input("총개수(개) 기본값", value=settings.get("default_quantity", "1"))
        with c3:
            default_count = st.text_input("나눌 묶음 수 기본값", value=settings.get("default_count", "1"))
        with c4:
            default_expiry_days = st.text_input("기한(일) 기본값", value=settings.get("default_expiry_days", ""))
        st.caption("무게 입력 방식에는 총무게 기본값이, 개수 입력 방식에는 총개수 기본값이 적용됩니다.")

        st.subheader("개인 메모장")
        user_memo = st.text_area("메모", value=settings.get("user_memo", ""), height=180)
        submitted = st.form_submit_button("설정 저장", use_container_width=True)

    if submitted:
        new_settings = {
            "fridge_list": parse_list(fridge_text, DEFAULT_SETTINGS["fridge_list"]),
            "category_list": parse_list(category_text, DEFAULT_SETTINGS["category_list"]),
            "default_weight": default_weight,
            "default_quantity": default_quantity or "1",
            "default_count": default_count or "1",
            "default_expiry_days": default_expiry_days,
            "user_memo": user_memo,
        }
        persist_settings(df, new_settings, storage_mode)
        st.success("설정을 저장했습니다.")
        st.rerun()

    st.divider()
    st.subheader("데이터 백업")
    csv_data = clean_inventory_df(df).to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        "현재 재고 데이터 CSV 다운로드",
        data=csv_data.encode("utf-8-sig"),
        file_name="portion_log_inventory_backup.csv",
        mime="text/csv",
        use_container_width=True,
    )


def page_help() -> None:
    st.title("❓ 사용 도움말")
    st.markdown(
        """
        ### 🍎 Portion-Log Cloud 사용 가이드

        **1. 식재료 등록**  
        대시보드의 등록 폼에 재고명, 수량 방식, 가격, 나눌 묶음 수, 유통기한을 입력한 뒤 `식재료 등록하기`를 누릅니다.
        나눌 묶음 수가 2개 이상이면 자동으로 `품목명 (1/3)` 형태로 나누어 저장됩니다.

        **2. 무게 입력과 개수 입력**  
        닭가슴살·고기처럼 무게로 관리하는 재료는 `무게(g)로 입력`, 계란·김·음료처럼 개수로 세기 쉬운 재료는 `개수(개)로 입력`을 사용하면 됩니다.

        **3. 상세 소분**  
        조각마다 무게가 다르거나 묶음별 개수가 다르면 `상세 소분`을 체크하고 묶음별 수량을 직접 입력합니다.
        손질 로스를 반영해 총무게보다 적게 소분할 수도 있습니다.

        **4. 상태 표시**  
        - 빨간 배지: 오늘 만료 또는 유통기한 3일 이내
        - 파란 취소선 배지: 기한 만료
        - 회색 배지: 유통기한 정보 없음

        **5. 소비 / 폐기 / 삭제**  
        재고 관리 화면의 모바일 카드 보기에서 각 품목을 `소비 처리`, `폐기 처리`, `삭제(통계 제외)` 상태로 변경할 수 있습니다.
        소비·폐기 금액은 상태값을 기준으로 자동 계산됩니다.

        **6. 저장과 최신 데이터 불러오기**  
        등록·수정·소비·폐기 작업은 Google Sheets에 자동 저장됩니다.
        다른 기기에서 변경한 내용을 현재 화면에 반영하려면 왼쪽 사이드바의 `최신 데이터 불러오기`를 누르세요.
        """
    )


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> None:
    inject_css()
    df, settings, storage_mode = load_data()

    st.sidebar.title("🍎 Portion-Log")
    show_storage_status(storage_mode)
    st.sidebar.caption("등록·수정·소비·폐기 시 자동 저장됩니다. 다른 기기에서 바꾼 내용은 아래 버튼으로 불러오세요.")

    if st.sidebar.button("최신 데이터 불러오기", use_container_width=True):
        clear_session_data()
        st.rerun()

    page = st.sidebar.radio(
        "메뉴",
        ["대시보드/등록", "재고 관리", "설정/메모", "도움말"],
        index=0,
    )

    st.sidebar.divider()
    st.sidebar.caption("모바일에서는 왼쪽 상단 메뉴 버튼으로 사이드바를 열 수 있습니다.")

    if page == "대시보드/등록":
        page_dashboard_and_register(df, settings, storage_mode)
    elif page == "재고 관리":
        page_inventory(df, settings, storage_mode)
    elif page == "설정/메모":
        page_settings(df, settings, storage_mode)
    else:
        page_help()


if __name__ == "__main__":
    main()
