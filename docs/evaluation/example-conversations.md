# Example conversations (real model)

Real transcripts from the live stack on 2026-10-06 08:59 UTC (commit `8b5e629`), office A in `cloud` mode with OpenAI `gpt-5.4-mini`, captured by the real-model sample run (`scripts/eval.py --set general --real-sample`). All documents are synthetic (`backend/tests/fixtures/general/`, `backend/tests/fixtures/`); no key or secret appears here. Answers are copied verbatim from the API (`answer.text`, claims, sources and coverage); only the sources list is reformatted. Each conversation is followed by what it shows, including where it is wrong. Scores for every item are in [real-model-sample.md](real-model-sample.md).

## Safe-room (ממ״ד) size average in Ramat Gan (AE1)

Item `GQ28` of `backend/eval/questions_general.yaml`, asked by `admin-a@demo.test`.

**שאלה:** מה גודל ממ״ד ממוצע ברמת גן?

*kind `combined`, provider `template`, mode `cloud`*

> נתון ראשוני, כולל ערכים שחולצו אוטומטית וטרם נבדקו בידי אדם: ממוצע גודל ממ״ד 12 מ״ר (מבוסס על 1 תצפיות, רמת גן).
> כיסוי: 15 מסמכים בתחום; נמצא ערך ב-2; ב-13 הנתון אינו מצוין; 1 ערכים ממתינים לבדיקה ולא נכללו.
>
> להלן הקטעים הרלוונטיים ביותר מתוך מסמכי המשרד:
> • דירת 4 חדרים בקומה השלישית בבניין בן שש קומות ברחוב המעגל בשכונת תל בנימין ברמת גן. [E2] (H2 synthetic ramatgan hamaagal, עמ׳ 1)
> • נווה ברמת גן. [E3] (H1 synthetic ramatgan irusim, עמ׳ 1, 2)
> • קומה: 3 | חדרים: 4 | שטח דירה (מ״ר): 92 | שטח ממ״ד (מ״ר): 9.5 | מספר מרפסות: 2 | שטח מרפסות (מ״ר): 11 | חניות: 1 | מחסן: אין [E4] (H2 synthetic ramatgan hamaagal, עמ׳ 1)
> • שומת מקרקעין — הירדן 30, רמת גן [E5] (H8 synthetic ramatgan hayarden, עמ׳ 1)

Sources:
  - [E1] H1 synthetic ramatgan irusim, עמ׳ 1 — ערך: 12 (preliminary)
  - [E2] H2 synthetic ramatgan hamaagal, עמ׳ 1
  - [E3] H1 synthetic ramatgan irusim, עמ׳ 1, 2
  - [E4] H2 synthetic ramatgan hamaagal, עמ׳ 1
  - [E5] H8 synthetic ramatgan hayarden, עמ׳ 1
  - [E6] H8 synthetic ramatgan hayarden, עמ׳ 1
  - [E7] H2 synthetic ramatgan hamaagal, עמ׳ 1

Coverage line: מגבלת כיסוי: 5 מסמכים דורשים בדיקה; 2 מסמכים שעיבודם נכשל. ייתכן שמסמכים אלה מכילים נתונים רלוונטיים, ולכן התוצאה אינה בהכרח כוללת את כל נתוני המשרד.

Limitations:
  - הנתון מחושב רק מהמסמכים שבתחום שבהם נמצא ערך, ואינו מתאר את כלל המאגר.
  - תשובת המודל לא עברה את בדיקות האימות, ולכן מוצגים הקטעים עצמם.

Context chips after the turn: נתון: גודל ממ״ד · מדד: ממוצע · עיר: רמת גן

**What this shows:** No price clarification was asked. The figure is labeled preliminary (model-extracted, not yet reviewed) and the coverage line shows the scope. It is **wrong against the answer key** (10.67 m² over 3 apartments): H2's value sits in a table cell the server rejected, and H3 gives inner dimensions in cm, which the server does not convert to an area. The composed explanation failed verification, so the passages are shown instead.

## Ceiling height, a follow-up that changes the place

Item `GQ48` of `backend/eval/questions_general.yaml`, asked by `admin-a@demo.test`.

**שאלה:** מה גובה התקרה הממוצע ברמת גן?

*kind `combined`, provider `cloud`, mode `cloud`*

> הנתון המבוקש טרם חולץ מהמסמכים או טרם אומת, ולכן אינו מוצג עדיין.
> כיסוי: 15 מסמכים בתחום; נמצא ערך ב-2; ב-13 הנתון אינו מצוין; 2 ערכים ממתינים לבדיקה ולא נכללו.
>
> הסקה (לא נאמר במפורש במסמכים): אין בראיות נתון שמייצג גובה תקרה ממוצע לכלל רמת גן. [E1] [E3]
> בראיונות/מסמכים שסופקו מופיעים גובהי תקרה נקודתיים של 2.60 מ׳, 2.75 מ׳, 2.80 מ׳ ו-3.05 מ׳, אך הם מתייחסים לנכסים ספציפיים ולא לעיר כולה. [E1] [E2] [E3] [E4] [E5]

Claims: `inferred` E1, E3; `explicit` E1, E2, E3, E4, E5

Sources:
  - [E1] H3 synthetic ramatgan arlozorov, עמ׳ 1
  - [E2] H5 synthetic givatayim hamaavak, עמ׳ 2
  - [E3] H1 synthetic ramatgan irusim, עמ׳ 1, 2
  - [E4] H6 synthetic telaviv benyehuda, עמ׳ 1
  - [E5] H5 synthetic givatayim hamaavak, עמ׳ 1, 2

Coverage line: מגבלת כיסוי: 5 מסמכים דורשים בדיקה; 2 מסמכים שעיבודם נכשל. ייתכן שמסמכים אלה מכילים נתונים רלוונטיים, ולכן התוצאה אינה בהכרח כוללת את כל נתוני המשרד.

Limitations:
  - הנתון מחושב רק מהמסמכים שבתחום שבהם נמצא ערך, ואינו מתאר את כלל המאגר.
  - חלק מהטענות נתמכות רק בחלקן בראיות שצוטטו; מומלץ לעיין במקורות.
  - התשובה חלקית. מה חסר: חסר מקור שמספק נתון ממוצע או מדגם מייצג של גובהי תקרה ברמת גן, או תוצאות חישוב מאומתות המבוססות על מדגם כזה.

Context chips after the turn: נתון: גובה התקרה · מדד: ממוצע · עיר: רמת גן

**שאלה:** ומה לגבי גבעתיים?

*kind `combined`, provider `cloud`, mode `cloud`*

> נמצאו קטעים קשורים, אך המידע המבוקש אינו נאמר בהם במפורש.
> כיסוי: 3 מסמכים בתחום; נמצא ערך ב-0; ב-3 הנתון אינו מצוין.
>
> בגבעתיים יש דירת 3 חדרים בקומה השנייה מתוך 4, בבניין ותיק ברחוב המאבק. [E3]
> לנכס בגבעתיים אין חניה ואין מחסן. [E3]
> לנכס בגבעתיים אין ממ״ד, ויש מקלט משותף בקומת הקרקע. [E3]
> לדירה בגבעתיים מרפסת חזית בשטח 7 מ״ר שנסגרה בתריסים ללא היתר בנייה. [E3]
> שנת הבנייה של הבניין בגבעתיים היא 1968. [E3]
> גובה התקרה בנכס בגבעתיים הוא 2.80 מ׳. [E3] [E6]
> בבניין בגבעתיים הותקנה מעלית בשנת 2019 ביוזמת הדיירים. [E3]
> בגבעתיים יש גם דירת 5 חדרים ברחוב שינקין בשכונת גבעת רמב״ם, בבניין בן 5 קומות שנבנה בשנת 2012 ובו מעלית. [E4]
> לדירה ברחוב שינקין בגבעתיים יש ממ״ד בשטח של כ-11 מ״ר, שתי מרפסות, חניה אחת ומחסן בשטח 7 מ״ר. [E4]
> שכונת גבעת קוזלובסקי בגבעתיים נמצאת בחלק הדרומי של העיר, והעיר מאופיינת בצפיפות בנייה גבוהה ובביקוש עקבי לדירות מגורים. [E5]

Claims: `explicit` E3; `explicit` E3; `explicit` E3; `explicit` E3; `explicit` E3; `explicit` E3, E6; `explicit` E3; `explicit` E4; `explicit` E4; `explicit` E5

Sources:
  - [E1] H6 synthetic telaviv benyehuda, עמ׳ 1
  - [E2] H1 synthetic ramatgan irusim, עמ׳ 1, 2
  - [E3] H5 synthetic givatayim hamaavak, עמ׳ 1, 2
  - [E4] H4 synthetic givatayim shenkin, עמ׳ 1
  - [E5] H5 synthetic givatayim hamaavak, עמ׳ 1
  - [E6] H5 synthetic givatayim hamaavak, עמ׳ 2

Coverage line: מגבלת כיסוי: 5 מסמכים דורשים בדיקה; 2 מסמכים שעיבודם נכשל. ייתכן שמסמכים אלה מכילים נתונים רלוונטיים, ולכן התוצאה אינה בהכרח כוללת את כל נתוני המשרד.

Limitations:
  - הנתון מחושב רק מהמסמכים שבתחום שבהם נמצא ערך, ואינו מתאר את כלל המאגר.

Context chips after the turn: נתון: גובה התקרה · מדד: ממוצע · עיר: גבעתיים

**שאלה:** ובתל אביב?

*kind `combined`, provider `cloud`, mode `cloud`*

> הנתון המבוקש טרם חולץ מהמסמכים או טרם אומת, ולכן אינו מוצג עדיין.
> כיסוי: 2 מסמכים בתחום; נמצא ערך ב-1; ב-1 הנתון אינו מצוין; 1 ערכים ממתינים לבדיקה ולא נכללו.
>
> בקטעי הראיות שסופקו אין מידע על נכס בתל אביב מעבר לדירה ברחוב בן יהודה בצפון הישן של תל אביב. [E1]
> לנכס שבתל אביב יש 3 חדרים, הוא בקומה השלישית בבניין בן 4 קומות ללא מעלית. [E1]
> הבניין בתל אביב נבנה בשנת 1958, גובה התקרה בדירה 3.05 מ׳, יש מרפסת רחוב בשטח 6 מ״ר, אין חניה, ויש מחסן בשטח 4 מ״ר. [E1]

Claims: `explicit` E1; `explicit` E1; `explicit` E1

Sources:
  - [E1] H6 synthetic telaviv benyehuda, עמ׳ 1
  - [E2] H5 synthetic givatayim hamaavak, עמ׳ 2
  - [E3] H1 synthetic ramatgan irusim, עמ׳ 1, 2
  - [E4] H3 synthetic ramatgan arlozorov, עמ׳ 1
  - [E5] H5 synthetic givatayim hamaavak, עמ׳ 1, 2

Coverage line: מגבלת כיסוי: 5 מסמכים דורשים בדיקה; 2 מסמכים שעיבודם נכשל. ייתכן שמסמכים אלה מכילים נתונים רלוונטיים, ולכן התוצאה אינה בהכרח כוללת את כל נתוני המשרד.

Limitations:
  - הנתון מחושב רק מהמסמכים שבתחום שבהם נמצא ערך, ואינו מתאר את כלל המאגר.

Context chips after the turn: נתון: גובה התקרה · מדד: ממוצע · עיר: תל אביב-יפו

**What this shows:** Each follow-up changed only the city (רמת גן → גבעתיים → תל אביב-יפו) and kept the attribute and the metric. No figure was shown: the extracted heights went to review (unit inferred), and the Givatayim value sits in a key/value table cell the server rejects. The model's own text lists the heights it saw, labeled as an inference, but its 'Ramat Gan' list mixes in heights from Givatayim (2.80) and Tel Aviv (3.05) documents: the search behind the explanation was not limited to the city.

## Planning status and comparing two versions (AE6)

Item `GQ22` of `backend/eval/questions_general.yaml`, asked by `admin-a@demo.test`.

**שאלה:** מה נכתב בשומה של שינקין 18 בגבעתיים על התכנית לתוספת קומה?

*kind `content`, provider `cloud`, mode `cloud`*

> בקטעי הראיות שסופקו לא מופיעה תכנית לתוספת קומה בשומת שינקין 18 בגבעתיים. [E1] [E2]

Claims: `explicit` E1, E2

Sources:
  - [E1] H4 synthetic givatayim shenkin, עמ׳ 1
  - [E2] H4 synthetic givatayim shenkin, עמ׳ 1
  - [E3] H4 synthetic givatayim shenkin, עמ׳ 1
  - [E4] H5 synthetic givatayim hamaavak, עמ׳ 1, 2
  - [E5] D6 synthetic givatayim conflicting ppsm, עמ׳ 1
  - [E6] H5 synthetic givatayim hamaavak, עמ׳ 1

Coverage line: מגבלת כיסוי: 5 מסמכים דורשים בדיקה; 2 מסמכים שעיבודם נכשל. ייתכן שמסמכים אלה מכילים נתונים רלוונטיים, ולכן התוצאה אינה בהכרח כוללת את כל נתוני המשרד.

Limitations:
  - טענה אחת הושמטה מהתשובה כי לא נמצאה לה תמיכה בראיות שצוטטו.

Context chips after the turn: נתון: התכנית לתוספת קומה · עיר: גבעתיים

**שאלה:** אילו הנחות השתנו בין הגרסאות?

*kind `content`, provider `cloud`, mode `cloud`*

> H4 synthetic givatayim shenkin; D6 synthetic givatayim conflicting ppsm: בגרסה הראשונה סוג הנכס הוא דירה, ובגרסה השנייה סוג הנכס הוא קוטג׳. [E1] [E7]
> H4 synthetic givatayim shenkin; D6 synthetic givatayim conflicting ppsm: בגרסה הראשונה השכונה היא גבעת רמב״ם, ובגרסה השנייה השכונה היא בורוכוב. [E1] [E7]
> H4 synthetic givatayim shenkin; D6 synthetic givatayim conflicting ppsm: בגרסה הראשונה המועד הקובע הוא 01/06/2023, ובגרסה השנייה המועד הקובע הוא 05/05/2024. [E1] [E7]
> H4 synthetic givatayim shenkin; D6 synthetic givatayim conflicting ppsm: בגרסה הראשונה שטח הנכס הוא 120 מ״ר נטו, ובגרסה השנייה שטח הנכס הוא 160 מ״ר רשום. [E1] [E7]
> H4 synthetic givatayim shenkin; D6 synthetic givatayim conflicting ppsm: בגרסה הראשונה כתובת הנכס היא שינקין 18, ובגרסה השנייה כתובת הנכס היא בורוכוב 40. [E1] [E7]
> H4 synthetic givatayim shenkin; D6 synthetic givatayim conflicting ppsm: הסקה (לא נאמר במפורש במסמכים): בגרסה הראשונה יש דירה עם 5 חדרים, בעוד שבגרסה השנייה אין בראיות תיאור מפורש של מספר החדרים בנכס עצמו. [E2] [E7]
> H4 synthetic givatayim shenkin; D6 synthetic givatayim conflicting ppsm: הסקה (לא נאמר במפורש במסמכים): בגרסה הראשונה הבניין נבנה בשנת 2012 ומצוין שיש בו מעלית, ובגרסה השנייה אין בראיות פירוט מקביל על שנת בנייה או מעלית. [E2] [E7]
> D6 synthetic givatayim conflicting ppsm; H4 synthetic givatayim shenkin: הסקה (לא נאמר במפורש במסמכים): מטרת השומה השתנתה: בגרסה השנייה היא לצורך תשלום היטל השבחה, ואילו בגרסה הראשונה המטרה אינה מופיעה בקטעים שסופקו. [E9] [E1] [E2]
> D6 synthetic givatayim conflicting ppsm; H4 synthetic givatayim shenkin: הסקה (לא נאמר במפורש במסמכים): בגרסה השנייה מופיע שווי נכס של 5,200,000 ₪ ובסיס מע״מ לא כולל מע״מ, ואילו בגרסה הראשונה לא מופיע שווי כזה בקטעים שסופקו. [E7] [E8] [E1] [E2]

Claims: `explicit` E1, E7; `explicit` E1, E7; `explicit` E1, E7; `explicit` E1, E7; `explicit` E1, E7; `inferred` E2, E7; `inferred` E2, E7; `inferred` E9, E1, E2; `inferred` E7, E8, E1, E2

Sources:
  - [E1] H4 synthetic givatayim shenkin, עמ׳ 1
  - [E2] H4 synthetic givatayim shenkin, עמ׳ 1
  - [E3] H4 synthetic givatayim shenkin, עמ׳ 1
  - [E4] H5 synthetic givatayim hamaavak, עמ׳ 1
  - [E5] H5 synthetic givatayim hamaavak, עמ׳ 1
  - [E6] D6 synthetic givatayim conflicting ppsm, עמ׳ 1
  - [E7] D6 synthetic givatayim conflicting ppsm, עמ׳ 1
  - [E8] D6 synthetic givatayim conflicting ppsm, עמ׳ 1
  - [E9] D6 synthetic givatayim conflicting ppsm, עמ׳ 1

Coverage line: מגבלת כיסוי: 5 מסמכים דורשים בדיקה; 2 מסמכים שעיבודם נכשל. ייתכן שמסמכים אלה מכילים נתונים רלוונטיים, ולכן התוצאה אינה בהכרח כוללת את כל נתוני המשרד.

Limitations:
  - טענה אחת הושמטה מהתשובה כי לא נמצאה לה תמיכה בראיות שצוטטו.
  - חלק מהטענות נתמכות רק בחלקן בראיות שצוטטו; מומלץ לעיין במקורות.

Cleared from the context: נתון: התכנית לתוספת קומה

Context chips after the turn: נתון: הנחות · עיר: גבעתיים

**What this shows:** Turn 1 missed the planning passage on page 2 (retrieval). Turn 2 is a real failure: the model chose the source handles of H4 and an unrelated document (D6) instead of the two versions of H4, so the 'versions' it compares are different appraisals. Shown as is.

## Building permits: why the value was reduced

Item `GQ17` of `backend/eval/questions_general.yaml`, asked by `admin-a@demo.test`.

**שאלה:** למה השמאית הפחיתה מהשווי בגלל סגירת המרפסת בדירה ברחוב המאבק 25?

*kind `content`, provider `cloud`, mode `cloud`*

> השמאית הפחיתה בגלל שסגירת המרפסת נעשתה ללא היתר בנייה. [E1] [E3]
> הסיבה להפחתה הייתה שהסגירה ללא היתר עלולה לחייב הריסה או הסדרה. [E1]

Claims: `explicit` E1, E3; `explicit` E1

Sources:
  - [E1] H5 synthetic givatayim hamaavak, עמ׳ 2
  - [E2] H5 synthetic givatayim hamaavak, עמ׳ 1
  - [E3] H5 synthetic givatayim hamaavak, עמ׳ 1, 2
  - [E4] D6 synthetic givatayim conflicting ppsm, עמ׳ 1
  - [E5] D6 synthetic givatayim conflicting ppsm, עמ׳ 1
  - [E6] H4 synthetic givatayim shenkin, עמ׳ 2

Coverage line: מגבלת כיסוי: 5 מסמכים דורשים בדיקה; 2 מסמכים שעיבודם נכשל. ייתכן שמסמכים אלה מכילים נתונים רלוונטיים, ולכן התוצאה אינה בהכרח כוללת את כל נתוני המשרד.

Limitations:
  - טענה אחת הושמטה מהתשובה כי לא נמצאה לה תמיכה בראיות שצוטטו.

Context chips after the turn: נתון: הסיבה להפחתה בשווי בגלל סגירת המרפסת · סוג הנתון: שווי שנקבע בשומות · עיר: גבעתיים · סוג נכס: דירה

**What this shows:** A content answer whose claims cite page 2 of the appraisal; one unsupported claim was dropped by the verifier and the answer says so.

## Missing information: a reasoned abstention

Item `GQ41` of `backend/eval/questions_general.yaml`, asked by `admin-a@demo.test`.

**שאלה:** מה גובה התקרה בדירה ברחוב המעגל 7?

*kind `content`, provider `extractive`, mode `cloud`, abstention `not_stated`*

> נמצאו קטעים קשורים, אך המידע המבוקש אינו נאמר בהם במפורש.
> להלן הקטעים הרלוונטיים ביותר מתוך מסמכי המשרד:
> • גובה התקרה בדירה 2.75 מ׳. [E1] (H1 synthetic ramatgan irusim, עמ׳ 1, 2)
> • גובה התקרה בדירה 3.05 מ׳, כמקובל בבנייה של התקופה. [E2] (H6 synthetic telaviv benyehuda, עמ׳ 1)
> • לדירה אין חניה צמודה, והחניה באזור היא בכחול-לבן ברחוב. [E3] (H3 synthetic ramatgan arlozorov, עמ׳ 1)
> • גובה תקרה | 2.80 מ׳ [E4] (H5 synthetic givatayim hamaavak, עמ׳ 2)

Sources:
  - [E1] H1 synthetic ramatgan irusim, עמ׳ 1, 2
  - [E2] H6 synthetic telaviv benyehuda, עמ׳ 1
  - [E3] H3 synthetic ramatgan arlozorov, עמ׳ 1
  - [E4] H5 synthetic givatayim hamaavak, עמ׳ 2
  - [E5] H2 synthetic ramatgan hamaagal, עמ׳ 1
  - [E6] H5 synthetic givatayim hamaavak, עמ׳ 1, 2

Coverage line: מגבלת כיסוי: 5 מסמכים דורשים בדיקה; 2 מסמכים שעיבודם נכשל. ייתכן שמסמכים אלה מכילים נתונים רלוונטיים, ולכן התוצאה אינה בהכרח כוללת את כל נתוני המשרד.

Limitations:
  - לפי הראיות שנמצאו אין בסיס מספיק לתשובה מלאה; מוצגים הקטעים הרלוונטיים.
  - מה חסר: במסמכים שסופקו יש תיאור של דירה ברחוב המעגל, אך לא מצוין מספר הבית 7 ולא מצוין גובה תקרה לדירה זו.

Context chips after the turn: נתון: גובה התקרה · סוג נכס: דירה

**What this shows:** The model reported that the passages do not state the datum; the server turned that into a `not_stated` abstention and showed the passages it searched.

## Price per m², a clarification answered in free text (AE5)

Item `GQ51` of `backend/eval/questions_general.yaml`, asked by `admin-a@demo.test`.

**שאלה:** מה המחיר או השווי למ״ר בחרוזים ב-2024?

*kind `clarification`, provider `template`, mode `cloud`*

> לאיזה נתון הכוונה?

> אפשרויות: מחירי עסקאות · שווי שנקבע בשומות

Context chips after the turn: נתון: מחיר למ״ר · מדד: ממוצע · עיר: רמת גן · שכונה: חרוזים · שנים: 2024

**שאלה:** התכוונתי לעסקאות

*kind `clarification`, provider `template`, mode `cloud`*

> לפי איזה תאריך לסנן את השנה?

> אפשרויות: תאריך העסקה · המועד הקובע של השומה שבה הופיעה העסקה

**תשובה להבהרה:** (כפתור: תאריך העסקה)

*kind `clarification`, provider `template`, mode `cloud`*

> ברשומות התואמות יש כמה בסיסי שטח, ואין לערבב אותם בממוצע. לפי איזה בסיס שטח לחשב?

> אפשרויות: ברוטו (17 רשומות) · נטו (14 רשומות) · אקוויוולנטי (11 רשומות) · רשום (8 רשומות)

**תשובה להבהרה:** (כפתור: ברוטו (17 רשומות))

*kind `clarification`, provider `template`, mode `cloud`*

> ברשומות התואמות יש כמה סוגי נכסים. לאיזה סוג נכס לחשב?

> אפשרויות: דירה (12 רשומות) · דופלקס (2 רשומות) · דירת גן (2 רשומות) · פנטהאוז (1 רשומות)

**תשובה להבהרה:** (כפתור: דירה (12 רשומות))

*kind `clarification`, provider `template`, mode `cloud`*

> ברשומות התואמות יש בסיסי מע״מ שונים. לפי איזה בסיס לחשב?

> אפשרויות: כולל מע״מ (10 רשומות) · לא כולל מע״מ (2 רשומות)

**תשובה להבהרה:** (כפתור: כולל מע״מ (10 רשומות))

*kind `numeric`, provider `template`, mode `cloud`, served from cache*

> מתוך הרשומות המאומתות שנקלטו במאגר המשרד (מחירי עסקאות): נמצאו 10 עסקאות ייחודיות — רמת גן, שכונת חרוזים, תאריך העסקה בשנת 2024, דירה, שטח ברוטו, כולל מע״מ.
> ממוצע מחירי המ״ר של הרשומות: 26,474.68 ₪ למ״ר.
> מחיר משוקלל (סך המחירים חלקי סך השטחים): 26,644.19 ₪ למ״ר.
> חציון: 26,218.75 ₪ למ״ר; טווח: 23,360.66–29,766.54 ₪ למ״ר.

Sources:
  - [E1] D12 synthetic harozim injected instruction, עמ׳ 1
  - [E2] D3 synthetic harozim table across pages, עמ׳ 1
  - [E3] D3 synthetic harozim table across pages, עמ׳ 1
  - [E4] D3 synthetic harozim table across pages, עמ׳ 2
  - [E5] D3 synthetic harozim table across pages, עמ׳ 2
  - [E6] D5 synthetic harozim mixed formats, עמ׳ 1
  - [E7] D9 synthetic harozim 30 comparables, עמ׳ 1
  - [E8] D9 synthetic harozim 30 comparables, עמ׳ 2
  - [E9] D9 synthetic harozim 30 comparables, עמ׳ 2
  - [E10] D9 synthetic harozim 30 comparables, עמ׳ 2

Coverage line: מגבלת כיסוי: 5 מסמכים דורשים בדיקה; 2 מסמכים שעיבודם נכשל. ייתכן שמסמכים אלה מכילים נתונים רלוונטיים, ולכן התוצאה אינה בהכרח כוללת את כל נתוני המשרד.

Limitations:
  - הנתון מבוסס רק על רשומות מאגר המשרד שאושרו, ואינו אומדן של כלל השוק.
  - לא בוצעו התאמות לשווי, למדד, לקומה או למאפייני נכס.

Context chips after the turn: נתון: מחיר למ״ר · מדד: ממוצע · סוג הנתון: מחירי עסקאות · עיר: רמת גן · שכונה: חרוזים · שנים: 2024 · שדה תאריך: תאריך העסקה · סוג נכס: דירה · בסיס שטח: ברוטו · בסיס מע״מ: כולל מע״מ

**What this shows:** The price path is answered by SQL over verified records with no model call; the typed reply "התכוונתי לעסקאות" resolved the pending data-kind clarification and data kind was not asked again. Four more result-changing clarifications followed (date field, area basis, property type, VAT basis), answered with the first button by the script; the final figure came from the cache of an earlier identical computation.

## Renovation: a stated absence is an answer

Item `GQ59` of `backend/eval/questions_general.yaml`, asked by `admin-a@demo.test`.

**שאלה:** האם הדירה ברחוב האירוסים 12 שופצה מאז האכלוס?

*kind `content`, provider `cloud`, mode `cloud`*

> הדירה ברחוב האירוסים 12 לא שופצה מאז האכלוס. [E1]

Claims: `explicit` E1

Sources:
  - [E1] H1 synthetic ramatgan irusim, עמ׳ 1, 2
  - [E2] H3 synthetic ramatgan arlozorov, עמ׳ 1
  - [E3] H1 synthetic ramatgan irusim, עמ׳ 1
  - [E4] H1 synthetic ramatgan irusim, עמ׳ 1
  - [E5] H7 synthetic telaviv benyehuda 2023, 2. תיאור הנכס והבניין
  - [E6] H5 synthetic givatayim hamaavak, עמ׳ 1, 2

Coverage line: מגבלת כיסוי: 5 מסמכים דורשים בדיקה; 2 מסמכים שעיבודם נכשל. ייתכן שמסמכים אלה מכילים נתונים רלוונטיים, ולכן התוצאה אינה בהכרח כוללת את כל נתוני המשרד.

Context chips after the turn: נתון: שיפוץ שבוצע בדירה · סוג נכס: דירה

**What this shows:** The document says no changes were made since occupancy; the answer states it with its source.
