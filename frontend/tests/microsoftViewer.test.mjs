import assert from 'node:assert/strict';
import test from 'node:test';
import { microsoftViewerUrl } from '../src/officeEmbedUrl.ts';

test('keeps Microsoft embed links inside the Viewer', () => {
  const oneDrive = 'https://onedrive.live.com/embed?resid=ABC';
  const sharePoint = 'https://contoso.sharepoint.com/sites/team/_layouts/15/Doc.aspx?action=embedview&sourcedoc=ABC';
  assert.equal(microsoftViewerUrl(oneDrive), oneDrive);
  assert.equal(microsoftViewerUrl('<iframe src="https://onedrive.live.com/embed?resid=ABC&amp;authkey=DEF"></iframe>'),
    'https://onedrive.live.com/embed?resid=ABC&authkey=DEF');
  assert.equal(microsoftViewerUrl(sharePoint), sharePoint);
});

test('converts a public Office file to the Microsoft Online Viewer', () => {
  for (const kind of ['docx', 'xlsx', 'pptx']) {
    const file = `https://files.example.com/report.${kind}?download=1`;
    const url = new URL(microsoftViewerUrl(file));
    assert.equal(url.hostname, 'view.officeapps.live.com');
    assert.equal(url.searchParams.get('src'), file);
  }
});

test('rejects local, insecure and unrelated URLs', () => {
  for (const value of [
    'http://files.example.com/file.docx',
    'https://127.0.0.1/file.docx',
    'https://localhost/file.xlsx',
    'https://onedrive.live.com.evil.com/embed?resid=ABC',
    'https://contoso.sharepoint.com/sites/team/Doc.aspx?action=embedview',
    'https://contoso.sharepoint.com/_layouts/15/settings.aspx?action=embedview',
    'https://example.com/file.pdf',
    'https://view.officeapps.live.com/op/embed.aspx?src=http%3A%2F%2Flocalhost%2Ffile.docx',
  ]) assert.throws(() => microsoftViewerUrl(value));
});
