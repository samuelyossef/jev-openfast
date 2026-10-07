(() => {
  if (window.__jevPrivacy) return;
  const secrets = new Set(), groups = new Map(), touched = new WeakSet(), versions = new WeakMap();
  let locationText=location.href, locationRevision=1;
  const editable = e => e && (['INPUT','TEXTAREA'].includes(e.tagName) || e.isContentEditable);
  const value = e => String(e.value ?? e.innerText ?? '');
  const sensitive = e => e?.type === 'password' || /one-time-code|current-password|new-password/.test(e?.autocomplete || '') ||
    (e?.tagName==='INPUT' && /otp|verification.?code|c[oó]digo|(?:^|[-_ ])code(?:$|[-_ ])/i.test(
      [e.id,e.name,e.getAttribute('aria-label'),e.placeholder].join(' ')));
  const remember = text => { if (text) secrets.add(String(text)); };
  const scrub = input => {
    let text = String(input ?? '');
    for (const secret of [...secrets].sort((a,b)=>b.length-a.length)) {
      for (const variant of new Set([secret, encodeURIComponent(secret)])) {
        if (variant.length > 2) text = text.split(variant).join('[protegido]');
        else {
          const escaped = variant.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
          text = text.replace(new RegExp('(?<![\\p{L}\\p{N}])'+escaped+'(?![\\p{L}\\p{N}])','gu'),'[protegido]');
        }
      }
    }
    return text;
  };
  const url = input => {
    try {
      const u = new URL(input,location.href);
      for (const key of [...u.searchParams.keys()]) if (/^(code|token|access_token|id_token|refresh_token|password|otp|verification_code|auth|authorization|state|session|sid|jwt|ticket|assertion|SAMLResponse|client_secret)$/i.test(key)) {
        remember(u.searchParams.get(key)); u.searchParams.set(key,'[protegido]');
      }
      if(u.username || u.password) { remember(u.username);remember(u.password);u.username=u.password='[protegido]'; }
      if (/access_token=|id_token=|password=|otp=|code=/.test(u.hash)) {
        for (const v of new URLSearchParams(u.hash.slice(1)).values()) remember(v);
        u.hash='[protegido]';
      }
      return scrub(u.href);
    } catch { return scrub(input); }
  };
  const mark = e => { if (editable(e)) { touched.add(e); remember(value(e)); } };
  const privateValue = e => {
    if (!touched.has(e) && !sensitive(e)) return scrub(value(e));
    const current=value(e), previous=versions.get(e);
    remember(current);
    const version=previous?.text===current ? previous.version : (previous?.version || 0)+1;
    versions.set(e,{text:current,version});
    return current ? `[preenchido pelo usuário:${version}]` : '';
  };
  const collect = () => {
    for (const e of document.querySelectorAll('input,textarea,[contenteditable="true"]'))
      if (touched.has(e) || sensitive(e)) privateValue(e);
    url(location.href);
  };
  window.__jevPrivacy = {
    manual:false, scrub, url, collect, value:privateValue, sensitive,
    locationVersion() { if(locationText!==location.href) { locationText=location.href;locationRevision++; } return locationRevision; },
    protected:()=>secrets.size>0 || [...document.querySelectorAll('input')].some(sensitive),
    protect:()=>mark(document.activeElement),
    remember(text,group) {
      const combined=(groups.get(group)||'')+text; groups.set(group,combined);
      remember(text); remember(combined);
    },
    backspace(group) { groups.set(group,(groups.get(group)||'').slice(0,-1)); },
  };
  document.addEventListener('focusin',e=>{if(window.__jevPrivacy.manual)mark(e.target)},true);
  document.addEventListener('input',e=>{
    if (window.__jevPrivacy.manual || sensitive(e.target)) mark(e.target);
  },true);
})();
