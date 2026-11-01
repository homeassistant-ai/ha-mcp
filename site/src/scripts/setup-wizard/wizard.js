import { clientsData, platformsData } from './data';
import { state, methodLabels, scopeLabels, remotePathLabels, remotePathsForMethod, needsPlatform, isRemoteOnly } from './state';
import { generateConfig } from './generate-config';

const setupStateParams = ['method', 'client', 'scope', 'platform', 'remote'];
let restoringState = false;

// Keep the address bar and the Setup navbar link shareable at every step.
// Forced/irrelevant choices are omitted so each URL has one canonical form.
function syncSetupUrl(mode = 'push') {
  if (restoringState) return;

  const url = new URL(window.location.href);
  setupStateParams.forEach(param => url.searchParams.delete(param));
  if (state.method) url.searchParams.set('method', state.method);
  if (state.client) url.searchParams.set('client', state.client.id);
  if (state.method !== 'stdio-local' && state.scope) url.searchParams.set('scope', state.scope);
  if (state.platform && needsPlatform()) url.searchParams.set('platform', state.platform.id);
  if (state.scope === 'remote' && state.remotePath) url.searchParams.set('remote', state.remotePath);

  const relativeUrl = `${url.pathname}${url.search}${url.hash}`;
  window.history[mode === 'replace' ? 'replaceState' : 'pushState']({}, '', relativeUrl);
  const setupNavLink = document.querySelector('[data-nav-id="setup"]');
  if (setupNavLink) setupNavLink.href = relativeUrl;
}

function setNum(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = String(val);
}

// Smooth scroll to a section with a slight delay for the animation
function scrollToSection(sectionId, delay = 100, moveFocus = false) {
  if (restoringState) return;
  setTimeout(() => {
    const section = document.getElementById(sectionId);
    if (section && !section.classList.contains('hidden')) {
      section.scrollIntoView({ behavior: 'smooth', block: 'start' });
      if (moveFocus) {
        const heading = section.querySelector('h2, h3');
        if (heading instanceof HTMLElement) {
          heading.tabIndex = -1;
          heading.focus({ preventScroll: true });
        }
      }
    }
  }, delay);
}

// Show/hide sections based on state
function updateSections(scrollTo = null) {
  const method = state.method;
  const isStdio = method === 'stdio-local';
  const hasMethod = !!method;
  const hasClient = !!state.client;
  const scope = isStdio ? 'local' : state.scope;
  const isLocalScope = scope === 'local';
  const isRemoteScope = scope === 'remote';

  const scopeShown = hasClient && !isStdio;
  const archShown = hasClient && !!scope;
  const platformShown = hasClient && !!scope && needsPlatform();
  const remotePathShown = hasClient && !!scope && isRemoteScope && !isStdio;

  const sections = {
    client: hasMethod,
    scope: scopeShown,
    architecture: archShown,
    platform: platformShown,
    remotePath: remotePathShown,
    config: shouldShowConfig(),
  };

  document.getElementById('section-client').classList.toggle('hidden', !sections.client);
  document.getElementById('section-scope').classList.toggle('hidden', !sections.scope);
  document.getElementById('section-architecture').classList.toggle('hidden', !sections.architecture);
  document.getElementById('section-platform').classList.toggle('hidden', !sections.platform);
  document.getElementById('section-remote-path').classList.toggle('hidden', !sections.remotePath);
  document.getElementById('section-config').classList.toggle('hidden', !sections.config);

  // Show/hide the matching architecture diagram
  document.getElementById('arch-local').classList.toggle('hidden', !isStdio);
  document.getElementById('arch-network').classList.toggle('hidden', !(!isStdio && isLocalScope));
  document.getElementById('arch-remote').classList.toggle('hidden', !(!isStdio && isRemoteScope));

  // The Docker platform card only applies to the local stdio run
  const dockerPlatformCard = document.querySelector('[data-platform="docker"]');
  if (dockerPlatformCard) dockerPlatformCard.classList.toggle('hidden', method !== 'stdio-local');

  // With the component's built-in webhook, remote access is the easy path —
  // the Advanced badge on the Remote scope card only fits the tunnel/proxy methods.
  const remoteScopeBadge = document.querySelector('[data-scope="remote"] .complexity-badge');
  if (remoteScopeBadge) {
    const quickRemote = method === 'ha-component';
    remoteScopeBadge.dataset.complexity = quickRemote ? 'quick' : 'advanced';
    remoteScopeBadge.textContent = quickRemote ? 'Quick' : 'Advanced';
  }

  // Context-specific platform help text
  const platformHelp = document.getElementById('platform-help');
  if (platformHelp) {
    if (isStdio) platformHelp.textContent = 'Choose how to run ha-mcp on your machine.';
    else if (method === 'uvx') platformHelp.textContent = 'Choose the operating system of the machine that will run the ha-mcp server.';
    else platformHelp.textContent = 'Choose the operating system of your client machine.';
  }

  // Show only the remote-path cards that apply to the chosen method
  if (remotePathShown) {
    const allowed = remotePathsForMethod(method);
    document.querySelectorAll('[data-remote-path]').forEach(card => {
      card.classList.toggle('hidden', !allowed.includes(card.dataset.remotePath));
    });
  }

  // Step numbers: method=1, client=2, scope=3 are static in the markup;
  // platform / remote-path / config are numbered dynamically by what shows.
  let n = scopeShown ? 3 : 2;
  if (platformShown) { n += 1; setNum('platform-step-num', n); }
  if (remotePathShown) { n += 1; setNum('remotepath-step-num', n); }
  if (sections.config) { n += 1; setNum('config-step-num', n); }

  // Breadcrumb badges (each shows the prior choice; clicking re-opens that step)
  const methodBadge = document.getElementById('selected-method');
  if (methodBadge) methodBadge.textContent = hasMethod ? (methodLabels[method] || method) : '';
  const clientBadge = document.getElementById('selected-client');
  if (clientBadge) clientBadge.textContent = hasClient ? state.client.name : '';
  const platCrumb = document.getElementById('selected-platform-prev');
  if (platCrumb) platCrumb.textContent = isStdio ? (hasClient ? state.client.name : '') : (state.scope ? scopeLabels[state.scope] : '');
  const rpCrumb = document.getElementById('selected-remotepath-prev');
  if (rpCrumb) rpCrumb.textContent = (platformShown && state.platform) ? state.platform.name : (state.scope ? scopeLabels[state.scope] : '');
  const finalCrumb = document.getElementById('selected-final');
  if (finalCrumb) {
    let ft = '';
    if (state.remotePath) ft = remotePathLabels[state.remotePath];
    else if (platformShown && state.platform) ft = state.platform.name;
    else if (state.scope) ft = scopeLabels[state.scope];
    finalCrumb.textContent = ft;
  }

  if (scrollTo) scrollToSection(scrollTo);
}

function shouldShowConfig() {
  if (!state.method || !state.client) return false;
  const isStdio = state.method === 'stdio-local';
  const scope = isStdio ? 'local' : state.scope;
  if (!scope) return false;
  if (needsPlatform() && !state.platform) return false;
  if (scope === 'remote' && !state.remotePath) return false;
  return true;
}

function resetWizard() {
  Object.assign(state, { method: null, client: null, scope: null, platform: null, remotePath: null });
  document.querySelectorAll('[data-server-method], [data-client], [data-scope], [data-platform], [data-remote-path]').forEach(c => {
    c.classList.remove('selected');
  });
  document.querySelectorAll('[data-client], [data-scope]').forEach(c => { c.disabled = false; c.title = ''; });
}

function clickChoice(attribute, value) {
  if (!value) return false;
  const choice = Array.from(document.querySelectorAll(`[${attribute}]`))
    .find(element => element.getAttribute(attribute) === value);
  if (!(choice instanceof HTMLButtonElement) || choice.disabled || choice.classList.contains('hidden')) return false;
  choice.click();
  return true;
}

function currentSetupSection() {
  if (shouldShowConfig()) return 'section-config';
  if (!state.method) return 'section-method';
  if (!state.client) return 'section-client';

  const scope = state.method === 'stdio-local' ? 'local' : state.scope;
  if (!scope) return 'section-scope';
  if (needsPlatform() && !state.platform) return 'section-platform';
  if (scope === 'remote' && !state.remotePath) return 'section-remote-path';
  return 'section-method';
}

// Re-run the normal selection handlers so shared links get the same
// filtering and derived output as an interactive visit. Invalid or stale
// combinations stop at the last valid step and are canonicalized away.
function restoreStateFromUrl() {
  const url = new URL(window.location.href);
  const params = url.searchParams;
  const shouldPositionWizard = !url.hash && setupStateParams.some(param => params.has(param));
  const requested = {
    method: params.get('method'),
    client: params.get('client'),
    scope: params.get('scope'),
    platform: params.get('platform'),
    remotePath: params.get('remote'),
  };

  restoringState = true;
  resetWizard();
  updateSections();

  if (clickChoice('data-server-method', requested.method)
    && clickChoice('data-client', requested.client)) {
    const scopeRestored = state.method === 'stdio-local'
      || clickChoice('data-scope', requested.scope);
    const platformRestored = !needsPlatform()
      || (scopeRestored && clickChoice('data-platform', requested.platform));
    if (scopeRestored && platformRestored && state.scope === 'remote'
      && remotePathsForMethod(state.method).includes(requested.remotePath)) {
      clickChoice('data-remote-path', requested.remotePath);
    }
  }

  restoringState = false;
  syncSetupUrl('replace');
  if (shouldPositionWizard) scrollToSection(currentSetupSection(), 0, true);
}

// Wire the picker tiles and restore any state carried in the URL.
export function initSetupWizard() {
  // Method selection (step 1)
  document.querySelectorAll('[data-server-method]').forEach(card => {
    card.addEventListener('click', () => {
      if (card.disabled) return;
      state.method = card.dataset.serverMethod;
      // Reset downstream selections (stdio-local has no scope step — force local)
      state.client = null;
      state.scope = state.method === 'stdio-local' ? 'local' : null;
      state.platform = null;
      state.remotePath = null;

      document.querySelectorAll('[data-server-method]').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      document.querySelectorAll('[data-scope], [data-platform], [data-remote-path]').forEach(c => c.classList.remove('selected'));

      // Filter clients by method: a local stdio server can't serve a client
      // that has no stdio transport (ChatGPT, Claude.ai, Open WebUI).
      document.querySelectorAll('[data-client]').forEach(cl => {
        cl.classList.remove('selected');
        const c = clientsData.find(x => x.id === cl.dataset.client);
        let disabled = false;
        let reason = '';
        if (state.method === 'stdio-local' && c && !c.transports.includes('stdio')) {
          disabled = true;
          reason = `${c.name} doesn't support stdio — pick an HTTP server method instead`;
        }
        cl.disabled = disabled;
        cl.title = reason;
      });

      updateSections('section-client');
      syncSetupUrl();
    });
  });

  // Client selection (step 2)
  document.querySelectorAll('[data-client]').forEach(card => {
    card.addEventListener('click', () => {
      if (card.disabled) return;
      state.client = clientsData.find(c => c.id === card.dataset.client);
      const isStdio = state.method === 'stdio-local';
      // Reset downstream selections (stdio-local has no scope step — force local)
      state.scope = isStdio ? 'local' : null;
      state.platform = null;
      state.remotePath = null;

      document.querySelectorAll('[data-client]').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      document.querySelectorAll('[data-scope], [data-platform], [data-remote-path]').forEach(c => c.classList.remove('selected'));

      // Filter scope: remote-only clients (ChatGPT, Claude.ai) can't use local
      document.querySelectorAll('[data-scope]').forEach(sc => {
        const scopeId = sc.dataset.scope;
        let disabled = false;
        let reason = '';
        if (scopeId === 'local' && isRemoteOnly(state.client.id)) {
          disabled = true;
          reason = `${state.client.name} requires HTTPS (remote)`;
        }
        sc.disabled = disabled;
        sc.title = reason;
      });

      updateSections(isStdio ? 'section-platform' : 'section-scope');
      syncSetupUrl();
    });
  });

  // Scope hover preview - shows the Architecture Overview section
  document.querySelectorAll('.scope-option').forEach(card => {
    card.addEventListener('mouseenter', () => {
      const scopeType = card.dataset.scope;
      const archSection = document.getElementById('section-architecture');
      // Only show on hover if not already selected (i.e., section is hidden)
      if (archSection.classList.contains('hidden')) {
        archSection.classList.remove('hidden');
        archSection.classList.add('hover-preview');
      }
      document.getElementById('arch-local').classList.add('hidden');
      document.getElementById('arch-network').classList.toggle('hidden', scopeType !== 'local');
      document.getElementById('arch-remote').classList.toggle('hidden', scopeType !== 'remote');
    });
    card.addEventListener('mouseleave', () => {
      const archSection = document.getElementById('section-architecture');
      // Only hide if it was shown by hover (not selected); a selected scope
      // gets its own diagram back, since the hover swapped it.
      if (archSection.classList.contains('hover-preview')) {
        archSection.classList.add('hidden');
        archSection.classList.remove('hover-preview');
      } else {
        updateSections();
      }
    });
  });

  // Scope selection (step 3)
  document.querySelectorAll('[data-scope]').forEach(card => {
    card.addEventListener('click', () => {
      if (card.disabled) return;
      state.scope = card.dataset.scope;
      state.platform = null;
      state.remotePath = null;

      document.querySelectorAll('[data-scope]').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      document.querySelectorAll('[data-platform], [data-remote-path]').forEach(c => c.classList.remove('selected'));

      let next;
      if (needsPlatform()) next = 'section-platform';
      else if (state.scope === 'remote') next = 'section-remote-path';
      else next = 'section-config';
      updateSections(next);
      if (next === 'section-config') generateConfig();
      syncSetupUrl();
    });
  });

  // Platform selection (step 4, when shown)
  document.querySelectorAll('[data-platform]').forEach(card => {
    card.addEventListener('click', () => {
      if (card.disabled) return;
      state.platform = platformsData.find(p => p.id === card.dataset.platform);
      state.remotePath = null;

      document.querySelectorAll('[data-platform]').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      document.querySelectorAll('[data-remote-path]').forEach(c => c.classList.remove('selected'));

      const scope = state.method === 'stdio-local' ? 'local' : state.scope;
      const next = scope === 'remote' ? 'section-remote-path' : 'section-config';
      updateSections(next);
      if (next === 'section-config') generateConfig();
      syncSetupUrl();
    });
  });

  // Remote-path selection (step 5, when scope = remote)
  document.querySelectorAll('[data-remote-path]').forEach(card => {
    card.addEventListener('click', () => {
      if (card.disabled) return;
      state.remotePath = card.dataset.remotePath;

      document.querySelectorAll('[data-remote-path]').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');

      updateSections('section-config');
      generateConfig();
      syncSetupUrl();
    });
  });

  // Breadcrumb badges: keep the badge's own choice, reset the step it heads
  document.getElementById('selected-method')?.addEventListener('click', () => {
    state.client = null;
    state.scope = state.method === 'stdio-local' ? 'local' : null;
    state.platform = null;
    state.remotePath = null;
    document.querySelectorAll('[data-client], [data-scope], [data-platform], [data-remote-path]').forEach(c => c.classList.remove('selected'));
    updateSections('section-client');
    syncSetupUrl();
  });

  document.getElementById('selected-client')?.addEventListener('click', () => {
    const isStdio = state.method === 'stdio-local';
    state.scope = isStdio ? 'local' : null;
    state.platform = null;
    state.remotePath = null;
    document.querySelectorAll('[data-scope], [data-platform], [data-remote-path]').forEach(c => c.classList.remove('selected'));
    updateSections(isStdio ? 'section-platform' : 'section-scope');
    syncSetupUrl();
  });

  document.getElementById('selected-platform-prev')?.addEventListener('click', () => {
    state.platform = null;
    state.remotePath = null;
    document.querySelectorAll('[data-platform], [data-remote-path]').forEach(c => c.classList.remove('selected'));
    updateSections('section-platform');
    syncSetupUrl();
  });

  document.getElementById('selected-remotepath-prev')?.addEventListener('click', () => {
    state.remotePath = null;
    document.querySelectorAll('[data-remote-path]').forEach(c => c.classList.remove('selected'));
    updateSections('section-remote-path');
    syncSetupUrl();
  });

  // Start over
  document.getElementById('start-over').addEventListener('click', () => {
    resetWizard();
    updateSections('section-method');
    syncSetupUrl();
  });

  restoreStateFromUrl();
  window.addEventListener('popstate', restoreStateFromUrl);
}
