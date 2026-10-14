import { lazy, Suspense, useEffect, useState, type FormEvent } from 'react';
import { useI18n } from './i18n';
import { microsoftViewerUrl } from './officeEmbedUrl';

type Document = { id: string; name: string; kind: string; origin: string; created_at: number; parent_id: string | null };
type Detail = { id: string; name: string; preview: string };
const token = document.querySelector<HTMLMetaElement>('meta[name="demo-token"]')?.content || '';
const LocalOfficePreview = lazy(() => import('./LocalOfficePreview'));

const labels = {
  'pt-BR': { title: 'Documentos', upload: 'Enviar DOCX, XLSX ou PPTX', create: 'Criar com Jev', name: 'Nome do documento',
    prompt: 'Descreva o documento que deseja criar', createButton: 'Criar DOCX', local: 'Arquivos locais', empty: 'Nenhum documento salvo.',
    revise: 'Pedir revisão ao Jev', reviseButton: 'Criar revisão', revisionNote: 'A revisão cria outra versão DOCX. O conteúdo é enviado ao modelo de IA.',
    download: 'Baixar', delete: 'Excluir', deleteConfirm: 'Excluir este documento local?', loading: 'Aguarde…',
    localNote: 'Arquivos locais ficam no JEV e são exibidos por um visualizador Office de código aberto. A visualização Microsoft exige um link acessível pelo serviço Microsoft.',
    online: 'Microsoft 365 Online', instruction: 'Cole um link de incorporação do OneDrive/SharePoint ou um link público HTTPS de DOCX, XLSX ou PPTX.',
    placeholder: 'Link do documento ou de incorporação', show: 'Visualizar', clear: 'Fechar', open: 'Abrir no Microsoft 365',
    frame: 'Visualização do documento no Microsoft 365', invalid: 'Use um link de incorporação Microsoft ou um arquivo Office público em HTTPS.',
    error: 'Não foi possível concluir a operação.' },
  en: { title: 'Documents', upload: 'Upload DOCX, XLSX or PPTX', create: 'Create with Jev', name: 'Document name',
    prompt: 'Describe the document to create', createButton: 'Create DOCX', local: 'Local files', empty: 'No saved documents.',
    revise: 'Ask Jev to revise', reviseButton: 'Create revision', revisionNote: 'A revision creates another DOCX version. Content is sent to the AI model.',
    download: 'Download', delete: 'Delete', deleteConfirm: 'Delete this local document?', loading: 'Please wait…',
    localNote: 'Local files stay in JEV and use an open-source Office viewer. Microsoft preview needs a link its service can access.',
    online: 'Microsoft 365 Online', instruction: 'Paste a OneDrive/SharePoint embed link or a public HTTPS DOCX, XLSX or PPTX URL.',
    placeholder: 'Document or embed link', show: 'View', clear: 'Close', open: 'Open in Microsoft 365',
    frame: 'Document preview in Microsoft 365', invalid: 'Use a Microsoft embed link or a public Office file over HTTPS.',
    error: 'The operation could not be completed.' },
  es: { title: 'Documentos', upload: 'Subir DOCX, XLSX o PPTX', create: 'Crear con Jev', name: 'Nombre del documento',
    prompt: 'Describa el documento', createButton: 'Crear DOCX', local: 'Archivos locales', empty: 'No hay documentos guardados.',
    revise: 'Pedir revisión a Jev', reviseButton: 'Crear revisión', revisionNote: 'Se crea otra versión DOCX. El contenido se envía al modelo de IA.',
    download: 'Descargar', delete: 'Eliminar', deleteConfirm: '¿Eliminar este documento local?', loading: 'Espere…',
    localNote: 'Los archivos locales quedan en JEV y usan un visor Office de código abierto. Microsoft necesita un enlace accesible.',
    online: 'Microsoft 365 Online', instruction: 'Pegue un enlace de OneDrive/SharePoint o un DOCX, XLSX o PPTX público en HTTPS.',
    placeholder: 'Enlace del documento', show: 'Ver', clear: 'Cerrar', open: 'Abrir en Microsoft 365',
    frame: 'Vista previa en Microsoft 365', invalid: 'Use un enlace Microsoft o un archivo Office público en HTTPS.',
    error: 'No se pudo completar la operación.' },
  fr: { title: 'Documents', upload: 'Importer DOCX, XLSX ou PPTX', create: 'Créer avec Jev', name: 'Nom du document',
    prompt: 'Décrivez le document', createButton: 'Créer DOCX', local: 'Fichiers locaux', empty: 'Aucun document enregistré.',
    revise: 'Demander une révision à Jev', reviseButton: 'Créer une révision', revisionNote: 'Une autre version DOCX est créée. Le contenu est envoyé au modèle IA.',
    download: 'Télécharger', delete: 'Supprimer', deleteConfirm: 'Supprimer ce document local ?', loading: 'Patientez…',
    localNote: 'Les fichiers locaux restent dans JEV et utilisent une visionneuse Office libre. Microsoft exige un lien accessible.',
    online: 'Microsoft 365 Online', instruction: 'Collez un lien OneDrive/SharePoint ou un DOCX, XLSX ou PPTX public en HTTPS.',
    placeholder: 'Lien du document', show: 'Afficher', clear: 'Fermer', open: 'Ouvrir dans Microsoft 365',
    frame: 'Aperçu dans Microsoft 365', invalid: 'Utilisez un lien Microsoft ou un fichier Office public en HTTPS.',
    error: 'Impossible de terminer l’opération.' },
};

async function result<T>(response: Response): Promise<T> {
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value as T;
}

export default function MicrosoftViewer() {
  const { locale } = useI18n();
  const text = labels[locale];
  const [documents, setDocuments] = useState<Document[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [input, setInput] = useState('');
  const [source, setSource] = useState('');
  const [name, setName] = useState('Documento.docx');
  const [prompt, setPrompt] = useState('');
  const [revision, setRevision] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function refresh() {
    const data = await result<{ documents: Document[] }>(await fetch('/api/documents', { cache: 'no-store' }));
    setDocuments(data.documents);
  }

  useEffect(() => { void refresh().catch((cause) => setError(String(cause))); }, []);

  async function open(id: string) {
    const data = await result<Detail>(await fetch(`/api/documents/${id}`, { cache: 'no-store' }));
    setDetail(data); setSource(''); setError('');
  }

  async function change(action: () => Promise<Document | null>) {
    setBusy(true); setError('');
    try {
      const created = await action();
      await refresh();
      if (created) await open(created.id);
    } catch (cause) { setError(cause instanceof Error ? cause.message : text.error); }
    finally { setBusy(false); }
  }

  async function sendJson(path: string, body: object): Promise<Document> {
    const response = await fetch(`/api/documents/${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Demo-Token': token }, body: JSON.stringify(body),
    });
    return (await result<{ document: Document }>(response)).document;
  }

  const show = (event: FormEvent) => {
    event.preventDefault();
    try { setSource(microsoftViewerUrl(input)); setDetail(null); setError(''); }
    catch { setError(text.invalid); }
  };

  return <div className="microsoft-viewer">
    <div className="microsoft-viewer-heading"><strong>{text.title}</strong>{busy && <span role="status">{text.loading}</span>}</div>
    <div className="document-actions">
      <form onSubmit={(event) => { event.preventDefault(); if (file) void change(async () => {
        const response = await fetch('/api/documents/upload', { method: 'POST',
          headers: { 'X-Demo-Token': token, 'X-File-Name': encodeURIComponent(file.name) }, body: file });
        return (await result<{ document: Document }>(response)).document;
      }); }}>
        <label htmlFor="document-upload">{text.upload}</label>
        <input id="document-upload" type="file" accept=".docx,.xlsx,.pptx" onChange={(event) => setFile(event.target.files?.[0] || null)} />
        <button type="submit" disabled={busy || !file}>{text.upload}</button>
      </form>
      <form onSubmit={(event) => { event.preventDefault(); void change(() => sendJson('create', { name, instruction: prompt })); }}>
        <strong>{text.create}</strong>
        <input aria-label={text.name} value={name} onChange={(event) => setName(event.target.value)} required />
        <textarea aria-label={text.prompt} value={prompt} onChange={(event) => setPrompt(event.target.value)} required maxLength={2000} />
        <button type="submit" disabled={busy || !prompt.trim()}>{text.createButton}</button>
      </form>
    </div>
    {error && <p className="microsoft-viewer-error" role="alert">{error}</p>}
    <section className="local-documents"><h3>{text.local}</h3><p className="microsoft-viewer-note">{text.localNote}</p>
      <div className="document-list">{documents.map((item) => <button key={item.id} type="button" className={detail?.id === item.id ? 'selected' : ''}
        onClick={() => { void open(item.id).catch((cause) => setError(String(cause))); }}>{item.name}</button>)}
        {!documents.length && <span>{text.empty}</span>}</div>
      {detail && <div className="document-detail"><div className="document-detail-head"><strong>{detail.name}</strong>
        <a href={`/api/documents/${detail.id}/download`} download={detail.name}>{text.download}</a>
        <button type="button" disabled={busy} onClick={() => { if (window.confirm(text.deleteConfirm)) void change(async () => {
          const response = await fetch('/api/documents/delete', { method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Demo-Token': token }, body: JSON.stringify({ id: detail.id }) });
          await result(response); setDetail(null); return null;
        }); }}>{text.delete}</button></div>
        <Suspense fallback={<p role="status">{text.loading}</p>}><LocalOfficePreview id={detail.id} name={detail.name} /></Suspense>
        <details><summary>Texto extraído</summary><pre className="document-preview">{detail.preview}</pre></details>
        {detail.name.toLowerCase().endsWith('.docx') && <form onSubmit={(event) => { event.preventDefault(); void change(() => sendJson('revise', { id: detail.id, instruction: revision })); }}>
          <label htmlFor="document-revision">{text.revise}</label>
          <textarea id="document-revision" value={revision} onChange={(event) => setRevision(event.target.value)} required maxLength={2000} />
          <small>{text.revisionNote}</small><button type="submit" disabled={busy || !revision.trim()}>{text.reviseButton}</button>
        </form>}</div>}
    </section>
    <section className="online-documents"><h3>{text.online}</h3><form onSubmit={show}><label htmlFor="microsoft-viewer-link">{text.instruction}</label>
      <div className="microsoft-viewer-form"><input id="microsoft-viewer-link" type="url" placeholder={text.placeholder} value={input} onChange={(event) => setInput(event.target.value)} required />
        <button type="submit">{text.show}</button></div></form>
      {source && <><div className="microsoft-viewer-frame"><iframe title={text.frame} src={source} referrerPolicy="no-referrer" allowFullScreen /></div>
        <div className="document-detail-head"><a href={source} target="_blank" rel="noopener noreferrer">{text.open}</a>
          <button type="button" onClick={() => setSource('')}>{text.clear}</button></div></>}
    </section>
  </div>;
}
