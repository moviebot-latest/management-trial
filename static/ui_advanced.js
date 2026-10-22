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

  // V15: do not flash a full-screen loader for instant/cached navigation.
  let loaderTimer=null;
  const originalShowPageLoader=window.showPageLoader;
  window.showPageLoader=function(text){
    clearTimeout(loaderTimer);
    loaderTimer=setTimeout(()=>originalShowPageLoader(text),120);
  };
  const originalHidePageLoader=window.hidePageLoader;
  window.hidePageLoader=function(){
    clearTimeout(loaderTimer);
    originalHidePageLoader();
  };
window.addEventListener('pageshow',()=>hidePageLoader());

  window.toggleSidebar=function(forceOpen){
    const sidebar=document.getElementById('sidebar');
    const menu=document.querySelector('.mobile-menu-btn');
    if(!sidebar) return;
    const open = typeof forceOpen === 'boolean' ? forceOpen : !sidebar.classList.contains('open');
    sidebar.classList.toggle('open',open);
    document.body.classList.toggle('sidebar-open',open && window.innerWidth<=768);
    if(menu){menu.setAttribute('aria-expanded',open?'true':'false');menu.setAttribute('aria-label',open?'Close menu':'Open menu');}
  };

  // Reliable mobile drawer controls: handle taps at the button itself, close on
  // outside tap / navigation / Escape, and keep ARIA state in sync.
  function setSidebarOpen(open){
    const sidebar=document.getElementById('sidebar');
    const menu=document.querySelector('.mobile-menu-btn');
    if(!sidebar) return;
    sidebar.classList.toggle('open',!!open);
    document.body.classList.toggle('sidebar-open',!!open && window.innerWidth<=768);
    if(menu){menu.setAttribute('aria-expanded',open?'true':'false');menu.setAttribute('aria-label',open?'Close menu':'Open menu');}
  }
  document.addEventListener('click',function(e){
    const menu=e.target.closest('.mobile-menu-btn');
    if(menu){e.preventDefault();setSidebarOpen(!document.getElementById('sidebar')?.classList.contains('open'));return;}
    const sidebar=document.getElementById('sidebar');
    if(sidebar && sidebar.classList.contains('open') && window.innerWidth<=768){
      if(!e.target.closest('#sidebar')) setSidebarOpen(false);
      else if(e.target.closest('a,button[type="submit"]')) setSidebarOpen(false);
    }
  });
  document.addEventListener('keydown',function(e){if(e.key==='Escape')setSidebarOpen(false);});
  window.addEventListener('resize',function(){if(window.innerWidth>768){document.body.classList.remove('sidebar-open');}});

  document.addEventListener('click',function(e){
    const a=e.target.closest('a');
    if(!a) return;
    const href=a.getAttribute('href')||'';
    if(!href || href.startsWith('#') || href.startsWith('javascript:') || a.target==='_blank' || a.hasAttribute('download')) return;
    if(href.startsWith('http') && !href.startsWith(location.origin)) return;
    showPageLoader(href.includes('logout')?'Signing you out securely…':href.includes('analytics')?'Loading analytics…':href.includes('settings')?'Opening settings…':href.includes('audit')?'Loading registrations…':'Opening securely…');
  },true);

  document.addEventListener('submit',function(e){
    const form=e.target;
    if(form.dataset.noLoader==='true' || form.id==='loginForm' || form.id==='regForm') return;
    const btn=form.querySelector('button[type="submit"]:last-of-type') || form.querySelector('button[type="submit"]');
    if(btn){btn.dataset.originalText=btn.innerHTML;btn.disabled=true;btn.innerHTML='<span class="btn-spinner"></span> Processing…';}
    showPageLoader('Processing securely…');
  },true);
})();
