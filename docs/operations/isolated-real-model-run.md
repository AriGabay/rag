# הרצה על מודל אמיתי במחסנית מבודדת

המסמך מתאר איך מפעילים מחסנית שנייה, מבודדת, ליד המחסנית של המשרד, ומריצים עליה מסמכים סינתטיים מול מודל אמיתי.
המחסנית המבודדת היא פרויקט compose עצמאי: שם משלה, פורטים משלה, volumes משלה ושמות images משלה. היא לא נוגעת
במסד הנתונים של המשרד, בקבצים שלו או ב־containers שלו. הסדר: בדיקת הקונפיגורציה, הפעלה, בדיקת גרסה, זריעה, הרצה,
כיבוי.

## מה מבודד ממה

| | מחסנית המשרד (`docker-compose.yml`) | מחסנית מבודדת (`docker-compose.isolated.yml`) |
|---|---|---|
| שם הפרויקט | `appraisal-rag` | `appraisal-rag-isolated` |
| מסד נתונים | `localhost:5433` | `localhost:15433` |
| backend | `localhost:8000` | `localhost:18000` |
| frontend | `localhost:3000` | `localhost:13000` |
| volumes | `appraisal-rag_pgdata`, `appraisal-rag_filedata` | `appraisal-rag-isolated_iso_pgdata`, `appraisal-rag-isolated_iso_filedata` |
| image של ה־backend | `appraisal-rag-backend` | `appraisal-rag-isolated-backend` |

- הקובץ המבודד שלם ועומד לבדו. משתמשים בו רק עם `-f docker-compose.isolated.yml`, בלי להניח אותו כשכבה על
  `docker-compose.yml`: ב־Compose 2.7.0 אין `!override`, ושכבה כזו הייתה מכניסה את הפורטים של המשרד.
- שם ה־image שונה בכוונה: בנייה במחסנית המבודדת לא מחליפה את ה־image שהמשרד רץ עליו.
- המסד המבודד נקרא גם הוא `rag`, אבל הוא בתוך container משלו ועל פורט משלו. בקרת הזריעה מבחינה ביניהם לפי הפורט
  והסימון (ראו "זריעה").
- ה־frontend נבנה עם `BACKEND_URL=http://backend:8000`, שם שירות שמתפענח ברשת של הפרויקט המבודד. ביומן הרשת
  בדפדפן מוודאים שהבקשות מגיעות ל־`localhost:13000` ולא ל־`localhost:3000`.

## 1. לפני ההרצה

1. קובץ הסביבה: מעתיקים את `real_documents/tests/round25/ops/isolated.env.example` ל־`isolated.env` באותה תיקייה
   ומחליפים כל `REPLACE_ME`:
   - הסיסמאות של מסד הנתונים והסוד של ה־session הם ערכים חדשים, שונים מאלה של `.env` של המשרד;
   - מפתח הספק מועתק ידנית מ־`.env`;
   - הקובץ לא נכנס ל־git ואין להדפיס את ערכיו. הסקריפט מסרב לעבוד אם `git check-ignore` לא מאשר שהקובץ
     מתעלמים ממנו, ואם נשאר בו `REPLACE_ME`.
2. בדיקת הקונפיגורציה, לפני כל `up` או זריעה. בדיקה כזו נכשלת אם מופיע פורט `5433`, `8000` או `3000` בצד המארח,
   שם volume של המשרד, או שם image של המשרד:

   ```bash
   scripts/isolated_stack.sh config-check
   ```

   אל תריצו `docker compose config` ישירות על קובץ הסביבה האמיתי: הפלט מכיל סודות. `config-check` לא מדפיס אותם.
   אפשר להריץ אותו גם על התבנית, כדי לבדוק רק תחביר: `scripts/isolated_stack.sh config-check
   real_documents/tests/round25/ops/isolated.env.example`.
3. מכינים את הספירות של המשרד לפני ההרצה (קריאה בלבד), כדי להוכיח בסוף שלא השתנה דבר: רשימת ה־containers
   (`docker ps`) וספירת שורות בטבלאות המסמכים והשיחות של מסד המשרד.

## 2. הפעלה

```bash
scripts/isolated_stack.sh up
```

הפקודה מריצה `config-check`, בונה דרך שירות `migrate` ואת ה־frontend, מפעילה את כל השירותים מחדש
(`--force-recreate`) וממתינה ל־`/api/health` ב־`localhost:18000`. `migrate` רץ ראשון ומעדכן את הסכמה, כי `backend`
ו־`worker` תלויים בו.

אחרי הבנייה בודקים קבוע שהשתנה בתוך ה־container, לפני כל מדידה. `docker compose build backend` משאיר את ה־image הישן,
ולכן בודקים שהקוד החדש אכן בפנים:

```bash
docker compose -p appraisal-rag-isolated -f docker-compose.isolated.yml \
  --env-file real_documents/tests/round25/ops/isolated.env \
  exec backend python -c "from app.chat.verify import JUDGE_VERSION; print(JUDGE_VERSION)"
```

הערך צריך להתאים לקוד שנבדק. אם לא, הבנייה לא הגיעה ל־image.

אין להריץ pytest בתוך ה־containers האלה. הבדיקות רצות על המארח בלבד, על מסד הבדיקות.

## 3. זריעה

```bash
scripts/isolated_stack.sh seed        # משרד ג׳ (מסמכי הערכה סינתטיים) ומשרדי הדמו א׳ וב׳
scripts/isolated_stack.sh seed eval   # משרד ג׳ בלבד
scripts/isolated_stack.sh seed demo   # משרדי הדמו בלבד, כולל משתמש של משרד ב׳ לבדיקת הפרדה בין משרדים
```

הסקריפט מעביר לתהליך הבן רק את `RAG_STACK=isolated`, את `BACKEND_URL` ואת `OWNER_DATABASE_URL` של המחסנית המבודדת.
סקריפטי הזריעה (`backend/scripts/seed_eval_office.py`, `backend/scripts/seed_demo.py`) מדפיסים בראשם את היעד
(כתובת ה־backend, המסד, הסימון) בלי סיסמאות, ומסרבים להמשיך כך:

- בלי `RAG_STACK=isolated` ובלי `--i-understand-office-target`: מסרבים תמיד, והודעת השגיאה מציינת אם היעד הוא פורט
  `8000`, פורט `5433` או מסד בשם `rag`;
- עם `RAG_STACK=isolated` ופורט `8000` או `5433` על המארח: מסרבים. הסימון לא מתיר את פורטי המשרד;
- זריעה מכוונת של המשרד היא רק עם `--i-understand-office-target`.

לפני העלאה מוודאים שהקבצים הם מסמכי ההערכה הסינתטיים של הריפו (`backend/tests/fixtures/eval_v5`, `eval_v6`,
`eval_v8`), ושום מסמך אמיתי לא נמצא במחסנית המבודדת. מסמכים אמיתיים נמצאים רק ב־`real_documents/` ובמסד המשרד.

## 4. הרצה והקלטה

מתעדים כל שלב כעבר או נכשל, עם מזהי הרצה וצילומי מסך, בקובץ פרטי תחת `real_documents/tests/round25/ops/`
(`scripts/isolated_stack.sh walkthrough-notes` יוצר קובץ עם הרשימה):

- העלאה והתקדמות העיבוד;
- שאלה בכל אחת משלוש קטגוריות: עובדה, טבלה, תנאי;
- פתיחת מקור מצוטט ובדיקה שההדגשה נוחתת על הבלוק המצוטט;
- שאלת המשך, מעבר נושא וחזרה;
- רענון הדף באמצע תשובה ואחריה;
- ניסיון חוזר, דיווח על תשובה בעייתית וייצוא הדיווח כמנהל;
- משתמש משרד ב׳ נדחה בשיחה, בהודעה, באבחון ובדיווח של משרד ג׳;
- כשל מכוון: מגבילים את `LLM_TIMEOUT_AGENT_SECONDS` בקובץ הסביבה לערך קצר מאוד, מפעילים מחדש רק את
  `backend` ו־`worker` (`docker compose -p appraisal-rag-isolated -f docker-compose.isolated.yml --env-file
  real_documents/tests/round25/ops/isolated.env up -d --force-recreate backend worker`), בודקים את נוסח הכשל,
  מחזירים את הערך, מפעילים מחדש ובודקים שניסיון חוזר מצליח.

כל קריאה למודל עולה כסף: שומרים על תקרת העלות של הסבב ועוצרים כשהיא קרובה.

## 5. אימות שהמשרד לא הושפע

- רשימת ה־containers של המשרד זהה לזו שנשמרה ב"לפני ההרצה", והם לא הופעלו מחדש.
- ספירות השורות במסד המשרד זהות (קריאה בלבד).
- `scripts/isolated_stack.sh status` מציג רק containers של `appraisal-rag-isolated`.

אל תריצו `scripts/backup.sh` או `scripts/restore.sh` כדי לגבות או לשחזר את המחסנית המבודדת: הם פועלים על הפרויקט
הנוכחי של compose, כלומר על המשרד.

## 6. כיבוי וניקוי

```bash
scripts/isolated_stack.sh down
```

`down` עוצר ומסיר את ה־containers והרשת בלבד. ה־volumes נשארים, ואפשר להפעיל שוב עם `up`. הסרת volumes היא פעולה
נפרדת ומפורשת, בשמות מלאים בלבד, וגם רק אחרי `down`:

```bash
scripts/isolated_stack.sh purge-volumes appraisal-rag-isolated_iso_pgdata appraisal-rag-isolated_iso_filedata
```

הסקריפט מסרב לכל שם שאינו אחד משני ה־volumes המבודדים, לשמות עם תווים כלליים ולרשימה ריקה, ובודק שה־volume נושא את
התווית של הפרויקט המבודד. הסרה מוחקת את הנתונים הסינתטיים של ההרצה בלבד.
