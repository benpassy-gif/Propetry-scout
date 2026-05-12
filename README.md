# 🏠 Property Scout v2 — Multi-Profile Edition

סוכן סריקה אוטומטי לנדל"ן ביוון עם תמיכה בפרופילים מרובים ו-Dashboard ויזואלי.

---

## ✨ מה חדש בגרסה 2

- **פרופילי חיפוש מרובים** — מנהל כמה חיפושים במקביל
- **Dashboard ויזואלי** — אתר ב-GitHub Pages לצפייה בכל התוצאות
- **שמירת תוצאות היסטוריות** — כל הנכסים שנמצאו נשמרים
- **סינון מתקדם** — חיפוש, סינון לפי פרופיל/אתר/ציון
- **טבלת Top Deals** — הנכסים הכי טובים בראש

---

## 📁 מבנה הפרויקט

```
Propetry-scout/
├── scraper/
│   ├── scout.py            ← הסוכן (קורא profiles.json)
│   ├── profiles.json       ← הפרופילים שלך
│   ├── results.json        ← תוצאות (נוצר אוטומטית)
│   └── requirements.txt
├── dashboard/
│   └── index.html          ← ה-Dashboard
└── .github/workflows/
    ├── scout.yml           ← מריץ את הסוכן
    └── deploy-dashboard.yml ← מפרסם ל-GitHub Pages
```

---

## 🚀 שדרוג מגרסה 1

אם יש לך כבר repository פעיל:

1. **מחק** את הקובץ הישן `scraper/scout.py`
2. **העלה** את כל הקבצים החדשים מהתיקייה הזו
3. **הפעל GitHub Pages:** Settings → Pages → Source: GitHub Actions
4. **דחוף Commit ראשון** — ה-workflow `deploy-dashboard` יפעל אוטומטית
5. **חכה 30 שניות** — ה-Dashboard יהיה זמין בכתובת `https://[username].github.io/[repo-name]/`

---

## ⚙️ ניהול פרופילים

ערוך את `scraper/profiles.json`. פרופיל לדוגמה:

```json
{
  "id": "my-flip-search",
  "name": "Flip Search Athens",
  "description": "Apartments to flip in central Athens",
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
    "min_deal_score": 3
  },
  "renovation_cost_per_sqm": 800,
  "target_mode": "flip"
}
```

**שדות חובה:**
- `id` — מזהה ייחודי (אנגלית, בלי רווחים)
- `name` — שם תצוגה
- `active` — true/false להפעלה/השבתה
- `filters` — כל הפילטרים

**שדות אופציונליים:**
- `description` — תיאור
- `renovation_cost_per_sqm` — עלות שיפוץ ל-מ"ר (ברירת מחדל: 800)
- `target_mode` — "flip" או "rent" (לצורכי ניתוח)

---

## 🎯 הפעלת פרופיל ספציפי

ב-GitHub: Actions → Property Scout → Run workflow → הכנס את ה-`id` בשדה Profile ID

---

## 🌐 הפעלת GitHub Pages

1. Settings → Pages
2. Source: **GitHub Actions**
3. בפעם הראשונה — לחץ "Run workflow" ב-Deploy Dashboard
4. ה-URL יופיע בעמוד

---

## ⏰ לוח זמנים

הסוכן רץ אוטומטית:
- **10:00** שעון אתונה — ראשון עד שישי
- **19:30** שעון אתונה — ראשון עד חמישי בלבד

לשינוי — ערוך `.github/workflows/scout.yml`
