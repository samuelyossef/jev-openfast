import { useId, useRef, useState, type FormEvent } from 'react';
import { useI18n } from './i18n';

type Props = {
  title: string;
  archived: boolean;
  disabled: boolean;
  onOpen: () => void;
  onRename: (title: string) => Promise<boolean>;
  onArchive: () => void;
  onDelete: () => Promise<boolean>;
};

export default function ConversationActions({ title, archived, disabled, onOpen, onRename, onArchive, onDelete }: Props) {
  const { t } = useI18n();
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const deleted = useRef(false);
  const [action, setAction] = useState<'rename' | 'delete'>('rename');
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(title);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  function closeMenu() {
    popover.current?.hidePopover();
    trigger.current?.focus();
  }

  function edit(next: 'rename' | 'delete') {
    closeMenu();
    setAction(next);
    setName(title);
    setError('');
    deleted.current = false;
    setRenaming(next === 'rename');
    requestAnimationFrame(() => {
      if (next === 'rename') { input.current?.focus(); input.current?.select(); }
      else dialog.current?.showModal();
    });
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (disabled || saving || (action === 'rename' && !name.trim())) return;
    if (action === 'rename' && name.trim() === title) { setRenaming(false); trigger.current?.focus(); return; }
    setSaving(true);
    setError('');
    try {
      const done = action === 'rename' ? await onRename(name.trim()) : await onDelete();
      if (done) {
        if (action === 'rename') { setRenaming(false); requestAnimationFrame(() => trigger.current?.focus()); }
        else { deleted.current = true; dialog.current?.close(); }
      }
      else setError(t('operationError'));
    } finally { setSaving(false); }
  }

  return <div className="conversation-actions">
    {renaming && <form className="conversation-inline-rename" onSubmit={submit}>
      <input ref={input} aria-label={t('conversationName')} value={name} maxLength={80} required
        readOnly={saving} aria-busy={saving} aria-invalid={!name.trim() || !!error}
        onChange={(event) => { setName(event.target.value); setError(''); }}
        onBlur={() => { if (!saving) { setRenaming(false); setError(''); } }}
        onKeyDown={(event) => {
          if (event.key === 'Escape' && !saving) { setRenaming(false); trigger.current?.focus(); }
        }} />
      {error && <p role="alert">{error}</p>}
    </form>}
    <button ref={trigger} type="button" className="icon-btn conversation-trigger" popoverTarget={id}
      disabled={disabled} aria-label={t('actionsFor', { title })} title={t('actionsFor', { title })}
      onClick={() => {
        const rect = trigger.current!.getBoundingClientRect();
        const menu = popover.current!;
        menu.style.left = `${Math.max(8, Math.min(rect.right - 224, window.innerWidth - 232))}px`;
        const top = Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - 280));
        menu.style.top = `${top}px`;
        menu.style.maxHeight = `${window.innerHeight - top - 8}px`;
      }}><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="3" cy="8" r="1" /><circle cx="8" cy="8" r="1" /><circle cx="13" cy="8" r="1" /></svg></button>
    <div ref={popover} id={id} popover="auto" className="conversation-menu" aria-label={t('actionsFor', { title })}>
      <p className="conversation-menu-title">{title}</p>
      <button type="button" disabled={disabled} onClick={() => { closeMenu(); onOpen(); }}>{t('openConversation')}</button>
      <button type="button" disabled={disabled} onClick={() => edit('rename')}>{t('rename')}</button>
      <button type="button" disabled={disabled} onClick={() => { closeMenu(); onArchive(); }}>{archived ? t('restoreConversation') : t('archiveConversation')}</button>
      <button type="button" className="danger" disabled={disabled} onClick={() => edit('delete')}>{t('deleteConversation')}</button>
    </div>
    <dialog ref={dialog} className="conversation-dialog" aria-labelledby={`${id}-heading`}
      onCancel={(event) => { if (saving) event.preventDefault(); }}
      onClose={() => { if (deleted.current) document.getElementById('new-chat')?.focus(); else trigger.current?.focus(); }}>
      <form onSubmit={submit}>
        <h2 id={`${id}-heading`}>{t('deleteConversation')}</h2>
        <p className="conversation-dialog-title">{title}</p><p>{t('deleteChat')}</p>
        {error && <p role="alert" className="danger">{error}</p>}
        <div className="conversation-dialog-buttons">
          <button type="button" disabled={saving} onClick={() => dialog.current?.close()}>{t('cancel')}</button>
          <button type="submit" className="danger-button" disabled={disabled || saving}>
            {saving ? t('saving') : t('delete')}</button>
        </div>
      </form>
    </dialog>
  </div>;
}
