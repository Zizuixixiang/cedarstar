
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, ClipboardCheck, FileText, Inbox, Mail, Plus, RefreshCw, Trash2, UserRound, X } from 'lucide-react';
import { apiFetch } from '../apiBase';
import './../styles/memory.css';
import './../styles/approvals.css';

const LABELS = {
  title: '\u5f85\u5ba1\u6279\u66f4\u65b0',
  refresh: '\u5237\u65b0',
  approve: '\u540c\u610f',
  reject: '\u62d2\u7edd',
  cancel: '\u53d6\u6d88',
  submitReject: '\u786e\u8ba4\u62d2\u7edd',
  rejectReason: '\u62d2\u7edd\u7406\u7531',
  rejectPlaceholder: '\u5199\u4e0b\u62d2\u7edd\u539f\u56e0',
  loading: '\u6b63\u5728\u8bfb\u53d6\u5f85\u5ba1\u6279\u5217\u8868...',
  empty: '\u6682\u65e0\u5f85\u5ba1\u6279\u9879',
  emptyMail: '\u6682\u65e0\u5f85\u5ba1\u6279\u90ae\u4ef6',
  emptyInbox: '\u6682\u65e0\u6536\u4ef6',
  approvalTab: '\u66f4\u65b0',
  mailTab: '\u90ae\u4ef6',
  inboxTab: '\u6536\u4ef6\u7bb1',
  contactsTab: '\u7b14\u53cb',
  contactName: '\u540d\u5b57',
  contactEmail: '\u90ae\u7bb1',
  contactNote: '\u5907\u6ce8',
  addContact: '\u65b0\u589e\u7b14\u53cb',
  deleteContact: '\u5220\u9664',
  emptyContacts: '\u6682\u65e0\u7b14\u53cb',
  createdAt: '\u53d1\u8d77\u65f6\u95f4',
  expiresIn: '\u5269\u4f59\u65f6\u95f4',
  requestSource: '\u53d1\u8d77\u6765\u6e90',
  expired: '\u5df2\u8fc7\u671f',
  before: 'Before',
  after: 'After',
  field: 'Field',
  unchanged: '\u672a\u53d8',
};

const TOOL_LABELS = {
  update_memory_card: '更新记忆卡片',
  update_temporal_state: '更新时效状态',
  update_relationship_timeline_entry: '更新关系时间线',
  update_persona_field: '更新人设字段',
  update_summary: '更新摘要',
  create_relationship_timeline_entry: '新增关系时间线条目',
  create_temporal_state: '新增时效状态',
  send_mail_outbox: '发送邮件',
};

const SHANGHAI_TIME_ZONE = 'Asia/Shanghai';

function parseDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDateTime(value) {
  const date = parseDate(value);
  if (!date) return '-';
  return date.toLocaleString('zh-CN', { timeZone: SHANGHAI_TIME_ZONE });
}

function formatCountdown(value, now) {
  const date = parseDate(value);
  if (!date) return '-';
  const ms = date.getTime() - now;
  if (ms <= 0) return LABELS.expired;
  const totalSeconds = Math.floor(ms / 1000);
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (days > 0) return `${days}d ${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
  return `${minutes}m ${seconds}s`;
}

function stableString(value) {
  if (value === undefined) return '';
  if (value === null) return 'null';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function sameValue(a, b) {
  return stableString(a) === stableString(b);
}

function displayValue(value) {
  const text = stableString(value);
  return text === '' ? '-' : text;
}

function approvalKeys(before, after) {
  const keys = Array.from(new Set([...Object.keys(before || {}), ...Object.keys(after || {})]));
  const priority = ['id', 'field_name', 'dimension', 'user_id', 'character_id', 'event_type'];
  const changed = keys.filter((key) => !sameValue(before?.[key], after?.[key]));
  const stable = priority.filter((key) => keys.includes(key) && !changed.includes(key));
  const orderedChanged = changed.sort((a, b) => priority.indexOf(b) - priority.indexOf(a));
  return Array.from(new Set([...stable, ...orderedChanged]));
}

function ApprovalDiff({ before, after }) {
  const keys = approvalKeys(before, after);
  if (!keys.length) {
    return <div className="approval-empty-diff">{LABELS.unchanged}</div>;
  }
  return (
    <div className="approval-diff-table">
      <div className="approval-diff-head approval-diff-row">
        <span>{LABELS.field}</span>
        <span>{LABELS.before}</span>
        <span>{LABELS.after}</span>
      </div>
      {keys.map((key) => {
        const changed = !sameValue(before?.[key], after?.[key]);
        return (
          <div className={`approval-diff-row ${changed ? 'is-changed' : ''}`} key={key}>
            <span className="approval-diff-field">{key}</span>
            <pre>{displayValue(before?.[key])}</pre>
            <pre>{displayValue(after?.[key])}</pre>
          </div>
        );
      })}
    </div>
  );
}

function ApprovalCard({ item, now, busyId, onApprove, onReject }) {
  const before = item.before_snapshot || {};
  const after = item.after_preview || {};
  const toolName = item.tool_name || '-';
  const toolLabel = TOOL_LABELS[toolName] || toolName;
  const source = item.requested_by_token_hash === 'internal_ai_tool' ? 'Internal AI' : (item.requested_by_token_hash ? 'MCP Token' : '-');
  const busy = busyId === item.id;

  return (
    <article className="approval-card">
      <div className="approval-card-header">
        <div className="approval-tool-title">
          <span className="approval-tool-icon" aria-hidden="true"><FileText size={18} strokeWidth={1.75} /></span>
          <div>
            <h3>{toolLabel}</h3>
            <p>{toolName}</p>
          </div>
        </div>
        <div className="approval-countdown">{formatCountdown(item.expires_at, now)}</div>
      </div>

      <div className="approval-meta-grid">
        <div>
          <span>{LABELS.createdAt}</span>
          <strong>{formatDateTime(item.created_at)}</strong>
        </div>
        <div>
          <span>{LABELS.expiresIn}</span>
          <strong>{formatCountdown(item.expires_at, now)}</strong>
        </div>
        <div>
          <span>{LABELS.requestSource}</span>
          <strong>{source}</strong>
        </div>
      </div>

      <ApprovalDiff before={before} after={after} />

      <div className="approval-actions">
        <button type="button" className="approval-button approval-button-approve" onClick={() => onApprove(item)} disabled={busy}>
          <Check size={16} strokeWidth={1.9} aria-hidden />
          <span>{LABELS.approve}</span>
        </button>
        <button type="button" className="approval-button approval-button-reject" onClick={() => onReject(item)} disabled={busy}>
          <X size={16} strokeWidth={1.9} aria-hidden />
          <span>{LABELS.reject}</span>
        </button>
      </div>
    </article>
  );
}

function MailApprovalCard({ item, busyId, onApprove, onReject }) {
  const mail = item.after_preview || {};
  const busy = busyId === item.id;
  return (
    <article className="approval-card mail-card">
      <div className="approval-card-header">
        <div className="approval-tool-title">
          <span className="approval-tool-icon" aria-hidden="true"><Mail size={18} strokeWidth={1.75} /></span>
          <div>
            <h3>{mail.subject || '\u65e0\u4e3b\u9898'}</h3>
            <p>{mail.to_name ? `${mail.to_name} <${mail.to_addr}>` : mail.to_addr}</p>
          </div>
        </div>
      </div>
      <pre className="mail-body-preview">{mail.body || ''}</pre>
      <div className="approval-actions">
        <button type="button" className="approval-button approval-button-approve" onClick={() => onApprove(item)} disabled={busy}>
          <Check size={16} strokeWidth={1.9} aria-hidden />
          <span>{LABELS.approve}</span>
        </button>
        <button type="button" className="approval-button approval-button-reject" onClick={() => onReject(item)} disabled={busy}>
          <X size={16} strokeWidth={1.9} aria-hidden />
          <span>{LABELS.reject}</span>
        </button>
      </div>
    </article>
  );
}

function InboxMailCard({ item }) {
  return (
    <article className="approval-card mail-card">
      <div className="approval-card-header">
        <div className="approval-tool-title">
          <span className="approval-tool-icon" aria-hidden="true"><Inbox size={18} strokeWidth={1.75} /></span>
          <div>
            <h3>{item.subject || '\u65e0\u4e3b\u9898'}</h3>
            <p>{item.from_name ? `${item.from_name} <${item.from_addr}>` : item.from_addr}</p>
          </div>
        </div>
        <div className="approval-countdown">{formatDateTime(item.received_at)}</div>
      </div>
      {item.summary && <div className="mail-summary">{item.summary}</div>}
      <pre className="mail-body-preview">{item.body || ''}</pre>
    </article>
  );
}

function ContactManager({ contacts, form, setForm, busyId, onSubmit, onDelete }) {
  return (
    <div className="mail-contact-layout">
      <form className="mail-contact-form" onSubmit={onSubmit}>
        <div className="mail-contact-grid">
          <label>
            <span>{LABELS.contactName}</span>
            <input
              value={form.name}
              onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
            />
          </label>
          <label>
            <span>{LABELS.contactEmail}</span>
            <input
              value={form.email}
              onChange={(event) => setForm((prev) => ({ ...prev, email: event.target.value }))}
              type="email"
              inputMode="email"
              required
            />
          </label>
        </div>
        <label className="mail-contact-note">
          <span>{LABELS.contactNote}</span>
          <textarea
            value={form.note}
            onChange={(event) => setForm((prev) => ({ ...prev, note: event.target.value }))}
            rows={3}
          />
        </label>
        <div className="approval-actions">
          <button type="submit" className="approval-button approval-button-approve" disabled={busyId === 'contact:create'}>
            <Plus size={16} strokeWidth={1.9} aria-hidden />
            <span>{LABELS.addContact}</span>
          </button>
        </div>
      </form>

      {contacts.length === 0 ? (
        <div className="approval-empty-state">{LABELS.emptyContacts}</div>
      ) : (
        <div className="mail-contact-list">
          {contacts.map((item) => (
            <article className="mail-contact-item" key={item.id}>
              <div className="approval-tool-title">
                <span className="approval-tool-icon" aria-hidden="true"><UserRound size={18} strokeWidth={1.75} /></span>
                <div>
                  <h3>{item.name || item.email}</h3>
                  <p>{item.email}</p>
                </div>
              </div>
              {item.note && <div className="mail-contact-item-note">{item.note}</div>}
              <button
                type="button"
                className="approval-button approval-button-reject"
                onClick={() => onDelete(item)}
                disabled={busyId === `contact:${item.id}`}
              >
                <Trash2 size={16} strokeWidth={1.9} aria-hidden />
                <span>{LABELS.deleteContact}</span>
              </button>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

function RejectDialog({ item, note, setNote, busy, onClose, onSubmit }) {
  if (!item) return null;
  return (
    <div className="approval-modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="approval-modal" role="dialog" aria-modal="true" aria-labelledby="approval-reject-title" onMouseDown={(e) => e.stopPropagation()}>
        <div className="approval-modal-header">
          <h3 id="approval-reject-title">{LABELS.reject}</h3>
          <button type="button" className="approval-icon-button" onClick={onClose} aria-label={LABELS.cancel}>
            <X size={18} strokeWidth={1.8} aria-hidden />
          </button>
        </div>
        <label className="approval-note-label" htmlFor="approval-reject-note">{LABELS.rejectReason}</label>
        <textarea
          id="approval-reject-note"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder={LABELS.rejectPlaceholder}
          rows={5}
          autoFocus
        />
        <div className="approval-modal-actions">
          <button type="button" className="approval-button" onClick={onClose} disabled={busy}>{LABELS.cancel}</button>
          <button type="button" className="approval-button approval-button-reject" onClick={onSubmit} disabled={busy}>
            <X size={16} strokeWidth={1.9} aria-hidden />
            <span>{LABELS.submitReject}</span>
          </button>
        </div>
      </section>
    </div>
  );
}

export default function Approvals() {
  const [activeTab, setActiveTab] = useState('approvals');
  const [approvals, setApprovals] = useState([]);
  const [inboxItems, setInboxItems] = useState([]);
  const [contacts, setContacts] = useState([]);
  const [contactForm, setContactForm] = useState({ name: '', email: '', note: '' });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busyId, setBusyId] = useState('');
  const [rejecting, setRejecting] = useState(null);
  const [rejectNote, setRejectNote] = useState('');
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const loadApprovals = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const res = await apiFetch('/api/approvals?status=pending');
      const payload = await res.json();
      if (!res.ok || !payload.success) {
        throw new Error(payload.message || `HTTP ${res.status}`);
      }
      setApprovals(Array.isArray(payload.data) ? payload.data : []);
    } catch (err) {
      setError(err?.message || 'failed');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadInbox = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const res = await apiFetch('/api/mail/inbox?limit=100');
      const payload = await res.json();
      if (!res.ok || !payload.success) {
        throw new Error(payload.message || `HTTP ${res.status}`);
      }
      setInboxItems(Array.isArray(payload.data) ? payload.data : []);
    } catch (err) {
      setError(err?.message || 'failed');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadContacts = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const res = await apiFetch('/api/mail/contacts');
      const payload = await res.json();
      if (!res.ok || !payload.success) {
        throw new Error(payload.message || `HTTP ${res.status}`);
      }
      setContacts(Array.isArray(payload.data) ? payload.data : []);
    } catch (err) {
      setError(err?.message || 'failed');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeTab === 'inbox') {
      loadInbox();
    } else if (activeTab === 'contacts') {
      loadContacts();
    } else {
      loadApprovals();
    }
  }, [activeTab, loadApprovals, loadContacts, loadInbox]);

  const normalApprovals = useMemo(
    () => approvals.filter((item) => item.tool_name !== 'send_mail_outbox'),
    [approvals],
  );
  const mailApprovals = useMemo(
    () => approvals.filter((item) => item.tool_name === 'send_mail_outbox'),
    [approvals],
  );
  const pendingCount = normalApprovals.length;
  const mailCount = mailApprovals.length;
  const contactCount = contacts.length;

  const removeApproval = useCallback((id) => {
    setApprovals((items) => items.filter((item) => item.id !== id));
  }, []);

  const approve = useCallback(async (item) => {
    setBusyId(item.id);
    setError('');
    try {
      const res = await apiFetch(`/api/approvals/${item.id}/approve`, { method: 'POST' });
      const payload = await res.json();
      if (!res.ok || !payload.success) {
        throw new Error(payload.message || `HTTP ${res.status}`);
      }
      removeApproval(item.id);
    } catch (err) {
      setError(err?.message || 'approve failed');
    } finally {
      setBusyId('');
    }
  }, [removeApproval]);

  const submitReject = useCallback(async () => {
    if (!rejecting) return;
    setBusyId(rejecting.id);
    setError('');
    try {
      const res = await apiFetch(`/api/approvals/${rejecting.id}/reject`, {
        method: 'POST',
        body: JSON.stringify({ note: rejectNote }),
      });
      const payload = await res.json();
      if (!res.ok || !payload.success) {
        throw new Error(payload.message || `HTTP ${res.status}`);
      }
      removeApproval(rejecting.id);
      setRejecting(null);
      setRejectNote('');
    } catch (err) {
      setError(err?.message || 'reject failed');
    } finally {
      setBusyId('');
    }
  }, [rejectNote, rejecting, removeApproval]);

  const submitContact = useCallback(async (event) => {
    event.preventDefault();
    setBusyId('contact:create');
    setError('');
    try {
      const res = await apiFetch('/api/mail/contacts', {
        method: 'POST',
        body: JSON.stringify(contactForm),
      });
      const payload = await res.json();
      if (!res.ok || !payload.success) {
        throw new Error(payload.message || `HTTP ${res.status}`);
      }
      setContactForm({ name: '', email: '', note: '' });
      await loadContacts();
    } catch (err) {
      setError(err?.message || 'contact save failed');
    } finally {
      setBusyId('');
    }
  }, [contactForm, loadContacts]);

  const deleteContact = useCallback(async (item) => {
    setBusyId(`contact:${item.id}`);
    setError('');
    try {
      const res = await apiFetch(`/api/mail/contacts/${item.id}`, { method: 'DELETE' });
      const payload = await res.json();
      if (!res.ok || !payload.success) {
        throw new Error(payload.message || `HTTP ${res.status}`);
      }
      setContacts((items) => items.filter((next) => next.id !== item.id));
    } catch (err) {
      setError(err?.message || 'contact delete failed');
    } finally {
      setBusyId('');
    }
  }, []);

  return (
    <div className="memory-container approvals-container">
      <div className="memory-tab-header approvals-header">
        <div className="memory-tab-header__title">
          <span className="memory-tab-header__icon" aria-hidden="true"><ClipboardCheck size={22} strokeWidth={1.75} /></span>
          <span className="memory-tab-header__title-text">{LABELS.title}</span>
          <span className="approval-count-badge">{activeTab === 'mail' ? mailCount : activeTab === 'inbox' ? inboxItems.length : activeTab === 'contacts' ? contactCount : pendingCount}</span>
        </div>
        <div className="memory-tab-header__actions">
          <button type="button" className="approval-button" onClick={activeTab === 'inbox' ? loadInbox : activeTab === 'contacts' ? loadContacts : loadApprovals} disabled={loading} title={LABELS.refresh}>
            <RefreshCw size={16} strokeWidth={1.8} aria-hidden />
            <span>{LABELS.refresh}</span>
          </button>
        </div>
      </div>

      <div className="approval-tabs" role="tablist" aria-label={LABELS.title}>
        <button type="button" className={`approval-tab ${activeTab === 'approvals' ? 'is-active' : ''}`} onClick={() => setActiveTab('approvals')}>
          <ClipboardCheck size={16} aria-hidden />
          <span>{LABELS.approvalTab}</span>
        </button>
        <button type="button" className={`approval-tab ${activeTab === 'mail' ? 'is-active' : ''}`} onClick={() => setActiveTab('mail')}>
          <Mail size={16} aria-hidden />
          <span>{LABELS.mailTab}</span>
        </button>
        <button type="button" className={`approval-tab ${activeTab === 'inbox' ? 'is-active' : ''}`} onClick={() => setActiveTab('inbox')}>
          <Inbox size={16} aria-hidden />
          <span>{LABELS.inboxTab}</span>
        </button>
        <button type="button" className={`approval-tab ${activeTab === 'contacts' ? 'is-active' : ''}`} onClick={() => setActiveTab('contacts')}>
          <UserRound size={16} aria-hidden />
          <span>{LABELS.contactsTab}</span>
        </button>
      </div>

      <div className="memory-content-scroll-area approvals-scroll-area">
        {error && <div className="approval-error">{error}</div>}
        {loading ? (
          <div className="tab-loading">{LABELS.loading}</div>
        ) : activeTab === 'inbox' ? (
          inboxItems.length === 0 ? (
            <div className="approval-empty-state">{LABELS.emptyInbox}</div>
          ) : (
            <div className="approval-list">
              {inboxItems.map((item) => <InboxMailCard key={item.id} item={item} />)}
            </div>
          )
        ) : activeTab === 'contacts' ? (
          <ContactManager
            contacts={contacts}
            form={contactForm}
            setForm={setContactForm}
            busyId={busyId}
            onSubmit={submitContact}
            onDelete={deleteContact}
          />
        ) : activeTab === 'mail' ? (
          mailApprovals.length === 0 ? (
            <div className="approval-empty-state">{LABELS.emptyMail}</div>
          ) : (
            <div className="approval-list">
              {mailApprovals.map((item) => (
                <MailApprovalCard
                  key={item.id}
                  item={item}
                  busyId={busyId}
                  onApprove={approve}
                  onReject={(next) => {
                    setRejecting(next);
                    setRejectNote('');
                  }}
                />
              ))}
            </div>
          )
        ) : normalApprovals.length === 0 ? (
          <div className="approval-empty-state">{LABELS.empty}</div>
        ) : (
          <div className="approval-list">
            {normalApprovals.map((item) => (
              <ApprovalCard
                key={item.id}
                item={item}
                now={now}
                busyId={busyId}
                onApprove={approve}
                onReject={(next) => {
                  setRejecting(next);
                  setRejectNote('');
                }}
              />
            ))}
          </div>
        )}
      </div>

      <RejectDialog
        item={rejecting}
        note={rejectNote}
        setNote={setRejectNote}
        busy={Boolean(busyId)}
        onClose={() => {
          if (!busyId) {
            setRejecting(null);
            setRejectNote('');
          }
        }}
        onSubmit={submitReject}
      />
    </div>
  );
}
