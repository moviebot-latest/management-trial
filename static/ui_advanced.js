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

  // Reliable mobile navigation: one handler controls the hamburger and drawer links.
  function setSidebarOpen(open){
    const sidebar=document.getElementById('sidebar');
    const menu=document.querySelector('.mobile-menu-btn');
    if(!sidebar)return;
    sidebar.classList.toggle('open',!!open);
    document.body.classList.toggle('sidebar-open',!!open);
    if(menu){menu.setAttribute('aria-expanded',open?'true':'false');menu.setAttribute('aria-label',open?'Close menu':'Open menu');}
  }
  window.toggleSidebar=function(){
    const sidebar=document.getElementById('sidebar');
    if(sidebar)setSidebarOpen(!sidebar.classList.contains('open'));
  };
  document.addEventListener('click',function(e){
    const menu=e.target.closest('.mobile-menu-btn');
    if(menu){
      e.preventDefault();
      const sidebar=document.getElementById('sidebar');
      if(sidebar)setSidebarOpen(!sidebar.classList.contains('open'));
      return;
    }
    const link=e.target.closest('#sidebar a.nav-item');
    if(link && window.innerWidth<=768){
      const href=link.getAttribute('href');
      if(!href || href==='#' || href.startsWith('javascript:'))return;
      // Keep normal browser navigation, but close the drawer before leaving.
      setSidebarOpen(false);
      window.location.assign(link.href);
      return;
    }
    if(document.body.classList.contains('sidebar-open') && window.innerWidth<=768 && !e.target.closest('#sidebar')){
      setSidebarOpen(false);
    }
  },true);
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape' && document.body.classList.contains('sidebar-open'))setSidebarOpen(false);
  });

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
