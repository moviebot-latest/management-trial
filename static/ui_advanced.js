(function(){
  'use strict';
  const overlay=document.createElement('div');
  overlay.className='global-loader';
  overlay.innerHTML='<div class="loader-orb"><div class="loader-ring"></div><div class="loader-logo">MS</div></div><div class="loader-title">Management System</div><div class="loader-sub" id="globalLoaderText">Loading securely…</div><div class="loader-progress"><span></span></div>';
  document.body.appendChild(overlay);
  window.showPageLoader=function(text){
    const t=document.getElementById('globalLoaderText'); if(t&&text)t.textContent=text;
    overlay.classList.add('show');
  };
  window.hidePageLoader=function(){overlay.classList.remove('show');};
  requestAnimationFrame(()=>document.body.classList.add('page-ready'));

  document.addEventListener('click',function(e){
    const a=e.target.closest('a');
    if(!a) return;
    const href=a.getAttribute('href')||'';
    if(!href || href.startsWith('#') || href.startsWith('javascript:') || a.target==='_blank' || a.hasAttribute('download')) return;
    if(href.startsWith('http') && !href.startsWith(location.origin)) return;
    showPageLoader(href.includes('logout')?'Signing you out securely…':'Opening page…');
  },true);

  document.addEventListener('submit',function(e){
    const form=e.target;
    if(form.dataset.noLoader==='true' || form.id==='loginForm') return;
    const btn=form.querySelector('button[type="submit"]:last-of-type') || form.querySelector('button[type="submit"]');
    if(btn){btn.dataset.originalText=btn.innerHTML;btn.disabled=true;btn.innerHTML='<span class="btn-spinner"></span> Processing…';}
    showPageLoader('Processing securely…');
  },true);
})();
