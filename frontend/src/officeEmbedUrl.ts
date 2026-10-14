const officeViewer = 'https://view.officeapps.live.com/op/embed.aspx';

function publicHttps(value: string): URL {
  const url = new URL(value);
  const host = url.hostname.toLowerCase();
  if (url.protocol !== 'https:' || url.username || url.password || url.hash ||
      host === 'localhost' || host.endsWith('.local') || host.includes(':') || /^\d+\.\d+\.\d+\.\d+$/.test(host)) {
    throw new Error('Use um link HTTPS público para o documento.');
  }
  return url;
}

export function microsoftViewerUrl(value: string): string {
  const embed = value.trim().match(/^<iframe\b[\s\S]*?\ssrc\s*=\s*(["'])(.*?)\1[\s\S]*?>$/i);
  const url = publicHttps(embed ? embed[2].replaceAll('&amp;', '&') : value.trim());
  const host = url.hostname.toLowerCase();
  if (host === 'onedrive.live.com' && url.pathname === '/embed' && url.search) return url.href;
  if (host.endsWith('.sharepoint.com') && /\/_layouts\/15\/(doc|guestaccess)\.aspx$/i.test(url.pathname) &&
      url.searchParams.get('action')?.toLowerCase() === 'embedview') return url.href;
  if (host === 'view.officeapps.live.com' && url.pathname === '/op/embed.aspx') {
    const source = url.searchParams.getAll('src');
    if (source.length === 1) publicHttps(source[0]);
    else throw new Error('O link do Viewer Microsoft não contém um arquivo válido.');
    return url.href;
  }
  if (/\.(docx|xlsx|pptx)$/i.test(url.pathname)) {
    return `${officeViewer}?src=${encodeURIComponent(url.href)}`;
  }
  throw new Error('Cole um link de incorporação do OneDrive/SharePoint ou um DOCX, XLSX ou PPTX público.');
}
