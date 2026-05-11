# 🏠 Property Scout — Greece

סוכן סריקה אוטומטי לנדל"ן ביוון.
מסרוק Spitogatos, XE.gr ו-Rightmove כל שעה ושולח התראות Telegram.

---

## 📁 מבנה הפרויקט

```
property-scout/
├── .github/
│   └── workflows/
│       └── scout.yml          ← GitHub Actions (מריץ כל שעה)
├── scraper/
│   ├── scout.py               ← הסוכן הראשי
│   └── requirements.txt
└── README.md
```

---

## 🚀 הגדרה — 5 שלבים

### שלב 1: צור Telegram Bot

1. פתח Telegram → חפש **@BotFather**
2. שלח `/newbot`
3. תן שם לבוט (לדוגמה: `MyPropertyScout`)
4. BotFather ישלח לך **Bot Token** — שמור אותו
5. שלח `/start` לבוט שלך
6. פתח: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
7. העתק את **chat_id** מהתשובה

---

### שלב 2: צור GitHub Repository

1. היכנס ל-[github.com](https://github.com) → **New repository**
2. שם: `property-scout` — Public או Private (שניהם עובדים)
3. העלה את כל הקבצים מהפרויקט הזה

---

### שלב 3: הגדר Secrets

בGitHub repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret Name | ערך |
|---|---|
| `TELEGRAM_BOT_TOKEN` | הטוקן מBotFather |
| `TELEGRAM_CHAT_ID` | ה-chat_id שלך |

---

### שלב 4: התאם את הפילטרים

ב-`scraper/scout.py`, ערוך את `FILTERS`:

```python
FILTERS = {
    "areas": [
        "nea-smyrni",      # נאה סמירני
        "kallithea",       # קאליתאה
        "palaio-faliro",   # פאלאיו פאלירו
        "glyfada",         # גליפאדה
        "ilioupoli",       # יליופולי
    ],
    "min_sqm": 40,
    "max_sqm": 120,
    "min_price": 40_000,
    "max_price": 150_000,
    "min_floor": -1,             # -1 = ημιυπόγειο
    "max_floor": 5,
    "max_price_per_sqm": 3_500,  # לא ישלח התראה מעל זה
}
```

**להוסיף אזורים** — השתמש ב-slug מהURL של Spitogatos:
```
spitogatos.gr/sale-flats/[SLUG-כאן]
```

---

### שלב 5: הפעל

**ריצה ידנית לבדיקה:**
- GitHub repo → **Actions** → **Property Scout** → **Run workflow**

**ריצה אוטומטית:**
- רץ כל שעה בין 07:00-23:00 שעון אתונה (בזכות ה-cron)

---

## 📱 פורמט הודעות Telegram

**נכס רגיל:**
```
🔥 Deal Score: 6/7 ████████░
📍 Nea Smyrni  |  Spitogatos
🏠 Διαμέρισμα 68τμ Εφέσου

💰 €72,000  |  68 מ"ר  |  €1,058/מ"ר
🏢 קומה 1
📊 vs שוק: -62.2%

🔨 שיפוץ מוערך: €54,400
📈 ROI פליפ: 18.3%
🏠 תשואה שכ"ד: 5.8%

🔗 [לפתיחת המודעה](...)
```

**נכס במכרז:**
```
⚖️ ΠΛΕΙΣΤΗΡΙΑΣΜΟΣ / מכרז

📍 Kallithea  |  XE.gr
💰 €55,000  |  75 מ"ר
...
```

---

## 🔧 התאמות נוספות

### שינוי תדירות סריקה
ב-`scout.yml`, ערוך את ה-cron:
```yaml
- cron: "0 4-20 * * *"    # כל שעה
- cron: "0 */2 4-20 * *"  # כל שעתיים
- cron: "0 7,13,19 * * *" # 3 פעמים ביום
```

### ציון מינימלי לשליחה
ב-`scout.py`, שנה:
```python
if listing.deal_score >= 3:  # הורד ל-1 לקבל הכל, העלה ל-5 לקבל רק מצוינות
```

### הוספת אזורים ל-benchmark
```python
AREA_BENCHMARKS = {
    "nea-smyrni": {"price_sqm": 2800, "rent_sqm": 12},
    # הוסף כאן...
}
```

---

## ⚠️ הערות חשובות

1. **Selectors עשויים להשתנות** — אתרים משנים את ה-HTML שלהם. אם הסוכן מפסיק לעבוד, בדוק את ה-CSS selectors ב-`scout.py`
2. **Rate limiting** — הסוכן ממתין 2 שניות בין בקשות. אל תקצר יותר מדי
3. **ממוצע שוק** — ה-benchmarks ב-`AREA_BENCHMARKS` הם אומדן. עדכן לפי נתונים עדכניים
4. **מכרזים** — זיהוי מכרז מבוסס על מילות מפתח. יתכנו false positives

---

## 💡 שלב הבא — Deal Analyzer

השתמש בקובץ `greece_deal_analyzer.jsx` לניתוח מעמיק של כל נכס שמעניין אותך.
