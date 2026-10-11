# עיבוד מחדש של מסמכים

עיבוד מחדש קורא שוב את המסמכים של המשרד בקורא הנוכחי: בלוקים, תמונות, טבלאות, עמודים, קטעי חיפוש וה־embeddings
שלהם. הרשומות והחלטות הסוקרים נשמרות. המסמך הזה מתאר את הסדר: גיבוי, הרצה, אימות ותרגיל שחזור.

## מה עיבוד מחדש עושה

לכל גרסה נוכחית של מסמך, עבודת `process:reindex` ב־`worker` מבצעת את השלבים האלה:

1. **קריאה ו־embeddings לפני כל כתיבה.** הקורא והמודל ל־embeddings רצים מחוץ לטרנזקציה. אם הקריאה נכשלת, דבר
   לא משתנה. זה כולל כשל זמני של הקריאה החזותית (timeout, rate limit, רשת): העבודה נכשלת ותנוסה שוב, והקריאה
   הקיימת נשארת כמו שהיא.
2. **בדיקת אי־נסיגה.** הקריאה החדשה מושווית לקריאה הקיימת, רק במדדים ששני הקוראים מפיקים:
   - כל מספר שמופיע בטקסט העמוד הקיים צריך להופיע גם בקריאה החדשה של אותו עמוד. נבדקים טקסט העמוד, הבלוקים
     והטבלאות. מילה מודגשת שקורא ישן קרא פעמיים (`2200,,550000`) נחשבת קיימת כשהקריאה החדשה מכילה את הגרסה הנקייה
     שלה (`20,500`);
   - עמוד שנקרא בהצלחה בקריאה הקיימת לא נכשל בקריאה החדשה;
   - מספר הטבלאות לא קטן.

   אזורים שהקורא החדש מדווח שלא נקראו או שנקראו בקריאה לא ודאית לא נחשבים נסיגה, כי הקורא הישן לא יכול היה
   לזהות אותם.
3. **נסיגה עוצרת את ההחלפה.** הקריאה הקיימת נשארת. הנסיגה נרשמת בגרסה (`document_versions.ingestion` ←
   `reprocess_regression`: העמודים והמספרים החסרים, העמודים שנכשלו ומספר הטבלאות), והעבודה מסתיימת מיד במצב
   `kept_previous`, עם סיכום ב־`jobs.last_error` וב־`ingestion.reprocess_kept`. המסמך נשאר זמין בלי אישור מנהל.
   קריאה חוזרת רגילה מדלגת על גרסה כזו. אפשר לאשר את הקריאה החדשה במפורש, כאפשרות בלבד (ראו "קריאה חדשה שאיבדה
   מידע" בהמשך).
4. **החלפה בטרנזקציה אחת.** טרנזקציה אחת מבצעת את כל השינויים יחד:
   - מוחקת את הקריאה הישנה ומכניסה את החדשה, כשה־embeddings כבר מחושבים. אין רגע שבו קטע קיים בלי embedding;
   - רושמת `reading_id` חדש ב־`ingestion`;
   - מעגנת מחדש את הנתונים הכמותיים;
   - מוחקת עובדות של המנוע הקודם שלא נבדקו (עובדה שנבדקה נשארת);
   - מנקה את `answer_cache` ומקדמת את גרסת הנתונים של המשרד.
5. **עיגון מחדש של נתונים כמותיים.**
   - ערך מטקסט נמצא שוב לפי הציטוט שלו, כשהערך מופיע בתוך הציטוט.
   - ערך מטבלה נמצא שוב לפי הכותרת שמציגה את הטבלה, כותרות העמודות ותווית השורה, כשהערך מופיע בשורה. האינדקס
     של הטבלה או של השורה אינו משמש לחיפוש.
   - לפני החיפוש מוחלים על הציטוט השמור התיקונים של הגרסה עצמה: מילה כפולה נקראת פעם אחת, ותו של מיפוי גופן פגום
     (למשל `ð`) יכול להתאים לכל אות שתיקוני הגופן המאושרים של הגרסה ממפים אותו אליה.
   - ערך שנבדק ולא נמצא שוב שומר את החלטת הסוקר. המיקום שלו מתאפס, ו־`measurements.anchor_lost` מתעד היכן הוא היה.
     ערך כזה מסומן בצ׳אט כלא מאומת מול הקריאה הנוכחית, ואינו משמש ראיה בבדיקת המשמעות.
   - ערך שלא נבדק ולא נמצא שוב נמחק. חילוץ הנתונים הכמותיים רץ אחרי ההחלפה, כשהמשרד מאפשר את מודל הענן.
6. **כל התשובות הקודמות במשרד מסומנות כמבוססות על מידע ישן**, כי גרסת הנתונים מתקדמת. זה צפוי.
   - ציטוט מתשובה קודמת שנפתח בחלון המקור מציג את הטקסט שצוטט בתשובה, עם הערה שהמסמך עובד מחדש ושהמיקום המדויק
     אינו זמין. שום בלוק לא מודגש.
   - הפניית `P#` לתשובה קודמת שמצביעה על קריאה ישנה לא נפתחת. הכלי מודיע למודל שההפניה היא לקריאה קודמת.

## 1. לפני ההרצה

1. אותה גרסה בכל השירותים. בונים מחדש ומפעילים מחדש. `migrate` רץ ראשון ומעדכן את הסכמה, כי `backend` ו־`worker`
   תלויים בו. גם ה־`frontend` נבנה מחדש, כי חלון המקור מציג ציטוט ישן כמיושן:

   ```bash
   docker compose build migrate && docker compose up -d --force-recreate backend worker
   docker compose up -d --build frontend
   ```

2. בדיקת חיבור לספק המודל, לפני כל עיבוד שמשתמש בקריאה חזותית: `scripts/check-model-egress.sh` (ראו
   [local-model-egress.md](local-model-egress.md)).

## 2. גיבוי

```bash
scripts/backup.sh
```

הסקריפט כותב ל־`backups/<timestamp>/` (התיקייה מחוץ ל־git):

- `rag.dump`: גיבוי PostgreSQL בפורמט custom;
- `files.tar.gz`: תוכן ה־volume של הקבצים;
- `alembic_version`: גרסת הסכמה;
- `readings.tsv`: לכל גרסה נוכחית, המזהה שלה, ה־`reading_id` וגרסת הקורא. רק מזהים, בלי תוכן;
- `SHA256SUMS`.

בודקים את הגיבוי לפני שממשיכים:

```bash
( cd backups/<timestamp> && shasum -a 256 -c SHA256SUMS )
```

לא מריצים עיבוד מחדש בלי גיבוי שעבר את הבדיקה.

## 3. הרצה

במסך הניהול, בחלק "קריאה מחדש של מסמכים":

- **קריאה מחדש של מסמכים שנקראו בגרסה ישנה.** רק גרסאות שהקורא שלהן שונה מהקורא הנוכחי. ל־PDF ול־DOCX יש גרסת
  קורא נפרדת. גרסה שקריאה חדשה שלה נמצאה נחותה (נסיגה) לא נכללת: קריאה חוזרת הייתה קוראת אותו דבר.
- **קריאה מחדש של כל המסמכים.**

הפעולות זמינות גם דרך ה־API (`POST /api/admin/reprocess`, עם `{"all": false}` או `{"all": true}`).

### קריאה חדשה שאיבדה מידע: הקריאה הקודמת נשמרת

אין צורך באישור מנהל כדי שהמסמך יישאר זמין. כשקריאה חדשה נחותה מהקודמת (נסיגה), או כשהקריאה נכשלת בכל הניסיונות
(למשל תקלה זמנית חוזרת בקריאה החזותית), העבודה מסתיימת במצב `kept_previous`: הקריאה הנוכחית נשארת כמו שהיא, זמינה
לחיפוש ולשיחה, והסיבה נרשמת בגרסה (`ingestion.reprocess_kept`: הסיבה, מספר הניסיונות והזמן) ומוצגת במסך המסמכים
ובמסך הניהול. נסיגה נרשמת גם ב־`ingestion.reprocess_regression`, עם סיכום של מה שחסר.

אישור הקריאה החדשה למרות הנסיגה הוא אפשרות בלבד, לא תנאי. משתמשים בה רק אחרי בדיקה של המסמך מול הסיכום, למשל כשמספר
שהקורא הישן קרא הפוך נקרא עכשיו נכון:

- במסך: "אישור הקריאות החדשות שנעצרו";
- דרך ה־API: `POST /api/admin/reprocess` עם `{"accept_regression": true}`.

האישור קורא שוב רק את הגרסאות שנרשמה להן נסיגה, ומחליף את הקריאה גם אם הנסיגה חוזרת. הנסיגה שאושרה נשמרת בדוח
של הקריאה החדשה (`ingestion.accepted_regression`).

### מיקומים במסמכים קיימים

קריאה חדשה שומרת מיקומים (גאומטריית עמוד, מיקום מילים ותאי טבלה) מעצמה. למסמכים שנקראו לפני כן, ההשלמה קוראת רק
את שכבת הטקסט של הקובץ השמור: בלי OCR, בלי קריאה חזותית ובלי מודל, ובלי לשנות את ה־`reading_id`, את מספרי הבלוקים
או את הטקסט, כך שהפניות קיימות נשארות תקפות. היא נכנסת לתור כשעמוד של מסמך כזה נפתח בתצוגת המקור, או לכל המשרד:

- דרך ה־API: `POST /api/admin/positions` (מנהל המשרד);
- ההתקדמות: `GET /api/admin/jobs` ← `positions` (כמה גרסאות יש להן מיקומים, וכמה בלוקים וטבלאות יושרו).

לוקחים גיבוי לפני הרצה על משרד שלם. קובץ שמור חסר מסיים את העבודה ב־`failed` בלי לשנות דבר.

## 4. אימות

1. **כל העבודות הסתיימו.** במסך הניהול אין עבודות פעילות. ב־SQL:

   ```sql
   SELECT kind, payload->>'mode' AS mode, status, count(*) FROM jobs GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
   SELECT version_id, status, last_error FROM jobs
   WHERE status IN ('failed', 'kept_previous') AND payload->>'mode' = 'reindex';
   SELECT id, ingestion->'reprocess_kept' FROM document_versions WHERE is_current AND ingestion ? 'reprocess_kept';
   ```

   עבודה במצב `kept_previous` השאירה את הקריאה הקודמת (ראו "קריאה חדשה שאיבדה מידע"): המסמך זמין, והסיבה רשומה
   ב־`reprocess_kept`. בודקים אם הסיבה היא נסיגה (יש גם `reprocess_regression`) או תקלה חוזרת, לפני שמחליטים אם לנסות
   שוב או לאשר. עבודה במצב `failed` היא שגיאה שצריך לבדוק. גם עבודת קריאה מחדש שה־worker שלה נפל בניסיון האחרון
   מסתיימת כרגע ב־`failed` (בלי `reprocess_kept`), והקריאה הקודמת נשארת גם אז. לא ממשיכים להערכה כשיש עבודה כזו
   שלא טופלה.
2. **כל הגרסאות נקראו בקורא הנוכחי, ולכל קטע יש embedding:**

   ```sql
   SELECT v.mime_type, v.ingestion->>'ingestion_version' AS reader, count(*),
          count(*) FILTER (WHERE v.ingestion ? 'reading_id') AS with_reading
   FROM document_versions v JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL
   WHERE v.is_current GROUP BY 1, 2;
   SELECT count(*) FROM chunks c JOIN document_versions v ON v.id = c.version_id AND v.is_current
   WHERE c.embedding IS NULL;
   ```

   השאילתה השנייה צריכה להחזיר 0. משווים מול `readings.tsv` של הגיבוי: לכל גרסה שעובדה יש `reading_id` חדש.
3. **נתונים כמותיים שאיבדו מיקום:**

   ```sql
   SELECT document_id, status, metric, value_text FROM measurements WHERE anchor_lost IS NOT NULL;
   ```

   ההחלטה של הסוקר נשמרת. כדאי לבדוק את הערכים האלה מחדש במסך הבדיקה.
4. **אותו build בכל השירותים.** `backend`, `worker` ו־`migrate` רצים מאותו image (`appraisal-rag-backend`). מזהה
   ה־image צריך להיות זהה ב־`backend` וב־`worker`:

   ```bash
   docker inspect --format '{{.Name}} {{.Image}}' $(docker compose ps -q backend worker)
   docker compose images
   ```

## 5. תרגיל שחזור

התרגיל משחזר את `rag.dump` למסד נפרד (`rag_drill`). הוא לא נוגע במסד `rag` ולא ב־volume של הקבצים.

> **אזהרה:** אל תשתמשו ב־`scripts/restore.sh` לתרגיל. הסקריפט עוצר את `backend` ואת `worker`, מוחק את המסד `rag`
> ויוצר אותו מחדש, ו**מוחק את כל תוכן ה־volume של הקבצים** לפני שהוא פורס את הארכיון. משתמשים בו רק לשחזור אמיתי.

1. יצירת המסד ושחזור הגיבוי אליו, כמו ב־`restore.sh` אבל בשם אחר:

   ```bash
   B=backups/<timestamp>
   docker compose exec -T db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
     -c "CREATE DATABASE rag_drill OWNER rag_owner ENCODING 'UTF8' LC_COLLATE 'en_US.utf8' LC_CTYPE 'en_US.utf8' TEMPLATE template0;"
   docker compose exec -T db pg_restore -U postgres -d rag_drill --exit-on-error < "$B/rag.dump"
   docker compose exec -T db psql -U postgres -d rag_drill -v ON_ERROR_STOP=1 \
     -c "GRANT CONNECT ON DATABASE rag_drill TO rag_app; GRANT USAGE ON SCHEMA public TO rag_app, rag_lookup;"
   ```

2. המסד המשוחזר מחזיק את הקריאות שבגיבוי:

   ```bash
   docker compose exec -T db psql -U postgres -d rag_drill -At -F "$(printf '\t')" -c \
     "SELECT id, COALESCE(ingestion->>'reading_id', ''), COALESCE(ingestion->>'ingestion_version', '')
      FROM document_versions WHERE is_current ORDER BY id" | diff - "$B/readings.tsv" && echo "readings match"
   docker compose exec -T db psql -U postgres -d rag_drill -At -c "SELECT version_num FROM alembic_version" \
     | diff - "$B/alembic_version" && echo "schema matches"
   ```

3. כניסה למערכת והגשת הקריאה הישנה. מריצים `backend` זמני על פורט אחר מול `rag_drill`. אין להריץ `worker` מול
   המסד הזה, ואין להריץ בדיקות (`pytest`) בתוך מכולה:

   ```bash
   set -a; . ./.env; set +a
   docker compose run --rm --no-deps -p 127.0.0.1:8001:8000 \
     -e DATABASE_URL="postgresql+psycopg://rag_app:${RAG_APP_PASSWORD}@db:5432/rag_drill" \
     -e OWNER_DATABASE_URL="postgresql+psycopg://rag_owner:${RAG_OWNER_PASSWORD}@db:5432/rag_drill" \
     backend uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

   בחלון אחר: מתחברים עם משתמש של המשרד (`POST http://127.0.0.1:8001/api/auth/login`). אחר כך מבקשים את הבלוקים
   של גרסה מתוך `readings.tsv`, עם ה־`reading_id` שלה מהגיבוי:
   `GET /api/documents/<document_id>/versions/<version_id>/blocks?reading_id=<reading_id>`
   (`none` כשהעמודה ריקה). התשובה צריכה להכיל `"stale": false` ובלוקים: זו הקריאה שגובתה.

4. ניקוי: עוצרים את ה־`backend` הזמני (Ctrl+C) ומוחקים את מסד התרגיל:

   ```bash
   docker compose exec -T db psql -U postgres -d postgres -c "DROP DATABASE rag_drill;"
   ```

## שחזור אמיתי

אם צריך לחזור לקריאה שלפני העיבוד: `scripts/restore.sh backups/<timestamp> --yes`. הסקריפט מחליף את המסד ואת
תוכן ה־volume של הקבצים בגיבוי. כל מה שנכתב אחרי הגיבוי אובד: מסמכים, שיחות והחלטות סוקרים.
