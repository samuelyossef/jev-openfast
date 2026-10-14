import { useEffect, useState } from 'react';
import FileViewer from '@file-viewer/react';
import officePreset from '@file-viewer/preset-office';

export default function LocalOfficePreview({ id, name }: { id: string; name: string }) {
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    setFile(null);
    setError('');
    void fetch(`/api/documents/${id}/download`, { signal: controller.signal, cache: 'no-store' })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return new File([await response.blob()], name);
      })
      .then(setFile)
      .catch((cause) => { if (!controller.signal.aborted) setError(String(cause)); });
    return () => controller.abort();
  }, [id, name]);

  if (error) return <p role="alert">{error}</p>;
  if (!file) return <p role="status">Carregando documento…</p>;
  return <div className="local-office-render"><FileViewer file={file} options={{
    preset: officePreset, rendererMode: 'replace', styleIsolation: 'shadow',
    toolbar: { position: 'bottom-right' },
  }} /></div>;
}
