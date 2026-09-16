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
window.addEventListener('pageshow',()=>hidePageLoader());

  window.toggleSidebar=function(){
    const sidebar=document.getElementById('sidebar');
    const menu=document.querySelector('.mobile-menu-btn');
    if(!sidebar) return;
    const open=!sidebar.classList.contains('open');
    sidebar.classList.toggle('open',open);
    if(menu){menu.setAttribute('aria-expanded',open?'true':'false');menu.setAttribute('aria-label',open?'Close menu':'Open menu');}
    document.body.classList.toggle('sidebar-open',open);
  };

  // Mobile sidebar: use delegated events so the hamburger works reliably
  // even when inline handlers/CSP or dynamically-rendered content are involved.
  document.addEventListener('click',function(e){
    const menu=e.target.closest('.mobile-menu-btn');
    if(menu){
      e.preventDefault();
      e.stopPropagation();
      const sidebar=document.getElementById('sidebar');
      if(!sidebar) return;
      const open=!sidebar.classList.contains('open');
      sidebar.classList.toggle('open',open);
      menu.setAttribute('aria-expanded',open?'true':'false');
      menu.setAttribute('aria-label',open?'Close menu':'Open menu');
      document.body.classList.toggle('sidebar-open',open);
      return;
    }
    const sidebar=document.getElementById('sidebar');
    if(sidebar && sidebar.classList.contains('open') && window.innerWidth <= 768){
      if(!e.target.closest('#sidebar')){
        sidebar.classList.remove('open');
        const menuBtn=document.querySelector('.mobile-menu-btn');
        if(menuBtn){menuBtn.setAttribute('aria-expanded','false');menuBtn.setAttribute('aria-label','Open menu');}
        document.body.classList.remove('sidebar-open');
      }
    }
  },true);

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
    if(form.dataset.noLoader==='true' || form.id==='loginForm') return;
    const btn=form.querySelector('button[type="submit"]:last-of-type') || form.querySelector('button[type="submit"]');
    if(btn){btn.dataset.originalText=btn.innerHTML;btn.disabled=true;btn.innerHTML='<span class="btn-spinner"></span> Processing…';}
    showPageLoader('Processing securely…');
  },true);
})();
