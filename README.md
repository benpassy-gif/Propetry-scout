# Property Scout v4.0 — Greece Real Estate Scout

סוכן סריקה אוטומטי לנדל"ן ביוון עם תמיכה בפרופילים מרובים, Dashboard ויזואלי ובוט Telegram אינטראקטיבי.

---

## מה חדש ב-v4.0

- **ללא כפילויות** — כל מודעה נשלחת פעם אחת בלבד. אם המחיר השתנה — תקבל התראה עם "PRICE CHANGE"
- **ללא הגבלת כמות** — כל הנכסים עם Score >= 4 נשלחים (לא עוד הגבלת 4 בריצה)
- **שתי ריצות ביום** — 07:00 + 19:00 שעון ישראל, ראשון עד שישי
- **בוט אינטראקטיבי** — ניהול פרופילים דרך Telegram (Cloudflare Worker)

---

## ארכיטקטורה

```
GitHub Actions (cron: 07:00 + 19:00 Israel)
    ↓
Python Scout (Playwright headless browser)
    ↓
Spitogatos + XE.gr + Rightmove Greece
    ↓
Deal Analysis + Deduplication (seen_listings.json)
    ↓
Telegram Bot → התראות
    ↓
Web Dashboard (GitHub Pages)
```

---

## מבנה הפרויקט

```
Propetry-scout/
├── scraper/
│   ├── scout.py              ← הסוכן הראשי (Playwright)
│   ├── profiles.json         ← פרופילי חיפוש
│   ├── results.json          ← תוצאות (נשמר אוטומטית)
│   ├── seen_listings.json    ← מעקב אחר מודעות שנשלחו
│   └── requirements.txt
├── dashboard/
│   └── index.html            ← Dashboard
└── .github/workflows/
    ├── scout.yml             ← cron + manual trigger
    └── deploy-dashboard.yml  ← פרסום ל-GitHub Pages
```

---

## ניהול פרופילים

ערוך `scraper/profiles.json` או השתמש בבוט Telegram.

```json
{
  "id": "my-flip-search",
  "name": "Flip Search Athens",
  "active": true,
  "filters": {
    "areas": ["nea-smyrni", "kallithea"],
    "min_sqm": 40,
    "max_sqm": 90,
    "min_price": 40000,
    "max_price": 100000,
    "min_floor": -1,
    "max_floor": 5,
    "max_price_per_sqm": 1800,
    "min_deal_score": 4
  },
  "renovation_cost_per_sqm": 800,
  "target_mode": "flip"
}
```

---

## הפעלת פרופיל ספציפי

ב-GitHub: Actions → Property Scout → Run workflow → הכנס את ה-`id` בשדה Profile ID

---

## לוח זמנים

- **07:00** שעון ישראל — ראשון עד שישי (04:00 UTC)
- **19:00** שעון ישראל — ראשון עד שישי (16:00 UTC)

---

## Secrets נדרשים ב-GitHub

- `TELEGRAM_BOT_TOKEN` — מ-BotFather
- `TELEGRAM_CHAT_ID` — מזהה הצ'אט שלך
