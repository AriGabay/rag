"use client";

import Link from "next/link";
import { useId, useState } from "react";
import { useSession } from "@/components/AppShell";
import { B, Dialog, ErrorAlert, Notice } from "@/components/ui";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { EFFECTIVE_PROVIDER_LABEL, formatTimestamp, versionStatusLabel } from "@/lib/format";
import type { AdminGroup, AdminUser, Role } from "@/lib/types";

const MIN_PASSWORD = 8;

// ---------------- Coverage ----------------

export function CoveragePanel() {
  const { data, error, reload } = useApi(api.coverage);

  return (
    <section className="card stack" aria-labelledby="cov-title">
      <h2 id="cov-title">כיסוי ועיבוד</h2>
      <ErrorAlert message={error} onRetry={reload} />
      {!data && !error && <p className="muted">טוען...</p>}
      {data && (
        <div className="grid-2">
          <div>
            <h3>מסמכים לפי סטטוס</h3>
            <dl className="kv">
              {Object.entries(data.documents_by_status).map(([status, count]) => (
                <div key={status} style={{ display: "contents" }}>
                  <dt>{versionStatusLabel(status)}</dt>
                  <dd>
                    <B>{count}</B>
                  </dd>
                </div>
              ))}
            </dl>
          </div>
          <div>
            <h3>רשומות</h3>
            <dl className="kv">
              <dt>סה״כ</dt>
              <dd>
                <B>{data.records.total}</B>
              </dd>
              <dt>מאומתות</dt>
              <dd>
                <B>{data.records.verified}</B>
              </dd>
              <dt>ממתינות לאימות</dt>
              <dd>
                <B>{data.records.awaiting_verification}</B>
              </dd>
              <dt>דורשות בדיקה</dt>
              <dd>
                <B>{data.records.needs_review}</B>
              </dd>
              <dt>חשדות לכפילות פתוחים</dt>
              <dd>
                <B>{data.open_dedup_candidates}</B>
              </dd>
              <dt>פריטים בתור הבדיקה</dt>
              <dd>
                <B>{data.review_queue_count}</B> · <Link href="/review">לתור הבדיקה</Link>
              </dd>
            </dl>
          </div>
        </div>
      )}
    </section>
  );
}

// ---------------- Provider settings ----------------

export function ProviderPanel() {
  const { data: settings, error: loadError, reload } = useApi(api.settings);
  // null = no local change; the checkbox shows the saved value.
  const [draftChoice, setDraftChoice] = useState<boolean | null>(null);
  const draft = draftChoice ?? settings?.cloud_llm_enabled ?? false;
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);

  async function save(enabled: boolean, acknowledge: boolean) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.saveSettings(enabled, acknowledge);
      setDialogOpen(false);
      setAck(false);
      setNotice("ההגדרה נשמרה.");
      setDraftChoice(null);
      reload();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  function onSave() {
    if (draft) {
      setAck(false);
      setDialogOpen(true);
    } else {
      void save(false, false);
    }
  }

  const providerName = settings?.provider_name || "ספק הענן";
  const providerText = settings?.model ? `${providerName} (${settings.model})` : providerName;

  return (
    <section className="card stack" aria-labelledby="prov-title">
      <h2 id="prov-title">מודל שפה בענן</h2>
      <ErrorAlert message={loadError} onRetry={reload} />
      <ErrorAlert message={error} />
      {notice && <Notice kind="ok">{notice}</Notice>}
      {!settings && !loadError && <p className="muted">טוען...</p>}
      {settings && (
        <>
          <dl className="kv">
            <dt>מצב בפועל</dt>
            <dd>
              <span className="badge badge-info" role="status" aria-label={`ספק בפועל: ${EFFECTIVE_PROVIDER_LABEL[settings.effective_provider] ?? settings.effective_provider}`}>
                {EFFECTIVE_PROVIDER_LABEL[settings.effective_provider] ?? settings.effective_provider}
              </span>
            </dd>
            <dt>ספק</dt>
            <dd>
              <bdi>{providerText}</bdi>
            </dd>
            <dt>אושר לאחרונה</dt>
            <dd>
              <B>{formatTimestamp(settings.acknowledged_at)}</B>
            </dd>
          </dl>
          <label className="checkbox">
            <input type="checkbox" checked={draft} onChange={(e) => setDraftChoice(e.target.checked)} />
            הפעלת מודל ענן לניסוח תשובות
          </label>
          <div className="row">
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || draft === settings.cloud_llm_enabled}
              onClick={onSave}
            >
              שמירה
            </button>
          </div>
        </>
      )}
      <Dialog open={dialogOpen} title="אישור שליחת מידע לספק חיצוני" onClose={() => setDialogOpen(false)}>
        <div className="stack">
          <p>
            בהפעלת האפשרות, קטעים רלוונטיים מתוך מסמכי המשרד יישלחו אל <strong><bdi>{providerText}</bdi></strong> כדי
            לנסח תשובות לשאלות. חישובי המספרים עצמם ממשיכים להתבצע במערכת.
          </p>
          <label className="checkbox">
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            קראתי ואני מאשר/ת שליחת קטעי מסמכים אל {providerName}
          </label>
          <div className="row">
            <button type="button" className="btn btn-primary" disabled={!ack || busy} onClick={() => void save(true, true)}>
              אישור והפעלה
            </button>
            <button type="button" className="btn" onClick={() => setDialogOpen(false)}>
              ביטול
            </button>
          </div>
        </div>
      </Dialog>
    </section>
  );
}

// ---------------- Groups ----------------

function GroupChecks({
  groups,
  value,
  onChange,
  legend,
}: {
  groups: AdminGroup[];
  value: string[];
  onChange: (ids: string[]) => void;
  legend: string;
}) {
  return (
    <fieldset style={{ border: "none", padding: 0, margin: 0 }}>
      <legend className="small">{legend}</legend>
      <div className="row">
        {groups.length === 0 && <span className="small muted">אין קבוצות</span>}
        {groups.map((g) => (
          <label key={g.id} className="checkbox">
            <input
              type="checkbox"
              checked={value.includes(g.id)}
              onChange={(e) => onChange(e.target.checked ? [...value, g.id] : value.filter((x) => x !== g.id))}
            />
            {g.name}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

export function GroupsPanel({ groups, onChanged }: { groups: AdminGroup[]; onChanged: () => void }) {
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) {
      setError("יש להזין שם קבוצה.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.createGroup(name.trim());
      setName("");
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card stack" aria-labelledby="groups-title">
      <h2 id="groups-title">קבוצות הרשאה</h2>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">שם</th>
              <th scope="col">מסמכים</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <tr key={g.id}>
                <td>{g.name}</td>
                <td>
                  <B>{g.document_count}</B>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <form className="row" onSubmit={create}>
        <label htmlFor="new-group" className="visually-hidden">
          שם קבוצה חדשה
        </label>
        <input
          id="new-group"
          className="input"
          placeholder="שם קבוצה חדשה"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <button type="submit" className="btn" disabled={busy}>
          הוספת קבוצה
        </button>
      </form>
      <ErrorAlert message={error} />
    </section>
  );
}

// ---------------- Users ----------------

function CreateUserForm({ groups, onCreated }: { groups: AdminGroup[]; onCreated: () => void }) {
  const uid = useId();
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("employee");
  const [canUpload, setCanUpload] = useState(false);
  const [groupIds, setGroupIds] = useState<string[]>([]);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const emailErr = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim()) ? null : "כתובת דוא״ל אינה תקינה.";
  const nameErr = fullName.trim() ? null : "יש להזין שם מלא.";
  const pwErr = password.length >= MIN_PASSWORD ? null : `סיסמה ראשונית באורך ${MIN_PASSWORD} תווים לפחות.`;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (emailErr || nameErr || pwErr) return;
    setBusy(true);
    setError(null);
    try {
      await api.createUser({
        email: email.trim(),
        full_name: fullName.trim(),
        password,
        role,
        can_upload: canUpload,
        group_ids: groupIds,
      });
      setEmail("");
      setFullName("");
      setPassword("");
      setRole("employee");
      setCanUpload(false);
      setGroupIds([]);
      setTouched(false);
      onCreated();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card stack" onSubmit={submit} noValidate aria-labelledby={`${uid}-t`}>
      <h3 id={`${uid}-t`}>משתמש חדש</h3>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <div className="field">
          <label htmlFor={`${uid}-email`}>דוא״ל</label>
          <input
            id={`${uid}-email`}
            className="input"
            type="email"
            dir="ltr"
            value={email}
            aria-invalid={touched && emailErr ? true : undefined}
            onChange={(e) => setEmail(e.target.value)}
          />
          {touched && emailErr && <span className="field-error">{emailErr}</span>}
        </div>
        <div className="field">
          <label htmlFor={`${uid}-name`}>שם מלא</label>
          <input
            id={`${uid}-name`}
            className="input"
            value={fullName}
            aria-invalid={touched && nameErr ? true : undefined}
            onChange={(e) => setFullName(e.target.value)}
          />
          {touched && nameErr && <span className="field-error">{nameErr}</span>}
        </div>
        <div className="field">
          <label htmlFor={`${uid}-pw`}>סיסמה ראשונית</label>
          <input
            id={`${uid}-pw`}
            className="input"
            type="password"
            dir="ltr"
            autoComplete="new-password"
            value={password}
            aria-invalid={touched && pwErr ? true : undefined}
            onChange={(e) => setPassword(e.target.value)}
          />
          {touched && pwErr && <span className="field-error">{pwErr}</span>}
        </div>
        <div className="field">
          <label htmlFor={`${uid}-role`}>תפקיד</label>
          <select id={`${uid}-role`} className="input" value={role} onChange={(e) => setRole(e.target.value as Role)}>
            <option value="employee">עובד/ת</option>
            <option value="admin">מנהל/ת</option>
          </select>
        </div>
      </div>
      <label className="checkbox">
        <input type="checkbox" checked={canUpload} onChange={(e) => setCanUpload(e.target.checked)} />
        הרשאת העלאת מסמכים
      </label>
      <GroupChecks groups={groups} value={groupIds} onChange={setGroupIds} legend="קבוצות" />
      <ErrorAlert message={error} />
      <div className="row">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          יצירת משתמש
        </button>
      </div>
    </form>
  );
}

function UserRow({ user, groups, isSelf, onChanged }: { user: AdminUser; groups: AdminGroup[]; isSelf: boolean; onChanged: () => void }) {
  const [editingGroups, setEditingGroups] = useState(false);
  const [groupDraft, setGroupDraft] = useState<string[]>(user.group_ids);
  const [pwOpen, setPwOpen] = useState(false);
  const [pw, setPw] = useState("");
  const [confirmDeactivate, setConfirmDeactivate] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function patch(body: Parameters<typeof api.updateUser>[1]) {
    setBusy(true);
    setError(null);
    try {
      await api.updateUser(user.id, body);
      onChanged();
      return true;
    } catch (err) {
      setError(errorMessage(err));
      return false;
    } finally {
      setBusy(false);
    }
  }

  const groupNames = groups.filter((g) => user.group_ids.includes(g.id)).map((g) => g.name);

  return (
    <tr>
      <td>
        {user.full_name}
        <div className="small muted">
          <bdi>{user.email}</bdi>
        </div>
      </td>
      <td>
        <label className="visually-hidden" htmlFor={`role-${user.id}`}>
          תפקיד עבור {user.full_name}
        </label>
        <select
          id={`role-${user.id}`}
          className="input"
          value={user.role}
          disabled={busy || isSelf}
          onChange={(e) => void patch({ role: e.target.value as Role })}
        >
          <option value="employee">עובד/ת</option>
          <option value="admin">מנהל/ת</option>
        </select>
      </td>
      <td>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={user.can_upload}
            disabled={busy}
            onChange={(e) => void patch({ can_upload: e.target.checked })}
          />
          <span className="visually-hidden">הרשאת העלאה עבור {user.full_name}</span>
        </label>
      </td>
      <td>
        {editingGroups ? (
          <div className="stack">
            <GroupChecks groups={groups} value={groupDraft} onChange={setGroupDraft} legend="קבוצות" />
            <div className="row">
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy}
                onClick={() => void patch({ group_ids: groupDraft }).then((ok) => ok && setEditingGroups(false))}
              >
                שמירה
              </button>
              <button type="button" className="btn" onClick={() => setEditingGroups(false)}>
                ביטול
              </button>
            </div>
          </div>
        ) : (
          <div>
            {groupNames.length > 0 ? groupNames.join(", ") : <span className="muted">ללא</span>}{" "}
            <button
              type="button"
              className="btn-link"
              onClick={() => {
                setGroupDraft(user.group_ids);
                setEditingGroups(true);
              }}
            >
              עריכה
            </button>
          </div>
        )}
      </td>
      <td>
        {user.is_active ? (
          <span className="badge badge-ok" role="status" aria-label="משתמש פעיל">
            פעיל
          </span>
        ) : (
          <span className="badge badge-danger" role="status" aria-label="משתמש מושבת">
            מושבת
          </span>
        )}
      </td>
      <td>
        <div className="row">
          {user.is_active ? (
            <button type="button" className="btn btn-danger" disabled={busy || isSelf} onClick={() => setConfirmDeactivate(true)}>
              השבתה
            </button>
          ) : (
            <button type="button" className="btn" disabled={busy} onClick={() => void patch({ is_active: true })}>
              הפעלה מחדש
            </button>
          )}
          <button type="button" className="btn" onClick={() => setPwOpen((v) => !v)} aria-expanded={pwOpen}>
            איפוס סיסמה
          </button>
        </div>
        {pwOpen && (
          <form
            className="row"
            style={{ marginBlockStart: 6 }}
            onSubmit={(e) => {
              e.preventDefault();
              if (pw.length < MIN_PASSWORD) {
                setError(`סיסמה באורך ${MIN_PASSWORD} תווים לפחות.`);
                return;
              }
              void patch({ password: pw }).then((ok) => {
                if (ok) {
                  setPw("");
                  setPwOpen(false);
                }
              });
            }}
          >
            <label className="visually-hidden" htmlFor={`pw-${user.id}`}>
              סיסמה חדשה עבור {user.full_name}
            </label>
            <input
              id={`pw-${user.id}`}
              className="input"
              type="password"
              dir="ltr"
              autoComplete="new-password"
              value={pw}
              onChange={(e) => setPw(e.target.value)}
            />
            <button type="submit" className="btn" disabled={busy}>
              שמירה
            </button>
          </form>
        )}
        <ErrorAlert message={error} />
        <Dialog open={confirmDeactivate} title="השבתת משתמש" onClose={() => setConfirmDeactivate(false)}>
          <p>
            השבתת {user.full_name} תנתק את כל החיבורים הפעילים שלו ותמנע כניסה. להמשיך?
          </p>
          <div className="row">
            <button
              type="button"
              className="btn btn-danger"
              onClick={() => {
                setConfirmDeactivate(false);
                void patch({ is_active: false });
              }}
            >
              השבתה
            </button>
            <button type="button" className="btn" onClick={() => setConfirmDeactivate(false)}>
              ביטול
            </button>
          </div>
        </Dialog>
      </td>
    </tr>
  );
}

async function loadUsersAndGroups() {
  const [u, g] = await Promise.all([api.users(), api.groups()]);
  return { users: u.users, groups: g.groups };
}

export function UsersAndGroups() {
  const me = useSession();
  const { data, error, reload } = useApi(loadUsersAndGroups);
  const users: AdminUser[] | null = data?.users ?? null;
  const groups: AdminGroup[] = data?.groups ?? [];
  const load = reload;

  return (
    <>
      <section className="card stack" aria-labelledby="users-title">
        <h2 id="users-title">משתמשים</h2>
        <ErrorAlert message={error} onRetry={load} />
        {!users && !error && <p className="muted">טוען...</p>}
        {users && (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">משתמש</th>
                  <th scope="col">תפקיד</th>
                  <th scope="col">העלאה</th>
                  <th scope="col">קבוצות</th>
                  <th scope="col">מצב</th>
                  <th scope="col">פעולות</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <UserRow key={u.id} user={u} groups={groups} isSelf={u.id === me?.user.id} onChanged={load} />
                ))}
              </tbody>
            </table>
          </div>
        )}
        <CreateUserForm groups={groups} onCreated={load} />
      </section>
      <GroupsPanel groups={groups} onChanged={load} />
    </>
  );
}
