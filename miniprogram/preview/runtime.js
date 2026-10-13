/**
 * 开发预览适配层，不冒充微信运行时。只实现本项目使用的 WXML 与 wx API 子集。
 * 模板表达式和模块均来自本机构建产物；API 返回文本只进入 textContent，绝不执行。
 */
import { binding, templateData } from './bindings.mjs?v=diagnosis1';
import { patchView } from './dom-update.mjs';
import morphdom from '/mini-dist/preview-vendor/morphdom.mjs';
const manifest = await fetch('/mini-dist/preview-manifest.json', { cache: 'no-store' }).then(r => { if (!r.ok) throw new Error('请先构建小程序'); return r.json(); });
const screen = document.querySelector('#screen'), tabs = document.querySelector('#tabs');
const pageStyle = document.createElement('style');
pageStyle.id = 'native-page-style'; document.head.append(pageStyle);
const cache = {};
let active, definition, currentPath = '', routeStack = [], toastTimer;
const stored = key => `fundscope-browser-preview:${key}`;
const clone = value => JSON.parse(JSON.stringify(value));
window.wx = {
  getStorageSync(key) { try { return JSON.parse(localStorage.getItem(stored(key))); } catch { return undefined; } },
  setStorageSync(key, value) { localStorage.setItem(stored(key), JSON.stringify(value)); },
  removeStorageSync(key) { localStorage.removeItem(stored(key)); },
  request(options) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), options.timeout || 60000);
    const chunks = [], headers = [];
    fetch(options.url, { method: options.method, headers: options.header, body: options.method === 'GET' ? undefined : JSON.stringify(options.data), credentials: 'omit', signal: controller.signal })
      .then(async response => {
        headers.forEach(fn => fn({header:Object.fromEntries(response.headers),statusCode:response.status}));
        if(options.enableChunked && response.ok && response.body){
          const reader=response.body.getReader();
          while(true){const {done,value}=await reader.read();if(done)break;const data=value.buffer.slice(value.byteOffset,value.byteOffset+value.byteLength);chunks.forEach(fn=>fn({data}));}
          options.success?.({statusCode:response.status,data:new ArrayBuffer(0)});return;
        }
        const raw = await response.text(); let data;
        try { data = JSON.parse(raw); } catch { data = raw; }
        options.success?.({ statusCode: response.status, data });
      }).catch(error => options.fail?.({ errMsg: error.message })).finally(() => clearTimeout(timer));
    return {abort:()=>controller.abort(),onChunkReceived:fn=>chunks.push(fn),onHeadersReceived:fn=>headers.push(fn)};
  },
  // 只下载页面已验证的后端媒体路径；浏览器端也不把令牌写进 URL。
  downloadFile(options){fetch(options.url,{headers:options.header,credentials:'omit'}).then(async r=>{const blob=await r.blob();options.success?.({statusCode:r.status,tempFilePath:URL.createObjectURL(blob)});}).catch(error=>options.fail?.({errMsg:error.message}));},
  setClipboardData({data}){navigator.clipboard.writeText(data).then(()=>wx.showToast({title:'已复制'})).catch(()=>wx.showToast({title:'浏览器未允许复制'}));},
  previewImage({current}){window.open(current,'_blank','noopener');},
  showModal(options) { options.success?.({ confirm: window.confirm(`${options.title}\n\n${options.content}`), cancel: false }); },
  showToast({ title }) { const box = document.querySelector('#toast'); box.textContent = title; box.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => box.hidden = true, 3000); },
  switchTab({ url }) { routeStack = []; navigate(url); },
  navigateTo({ url }) { routeStack.push(currentPath); navigate(url); },
  redirectTo({ url }) { navigate(url); },
  navigateBack() { navigate(routeStack.pop() || '/pages/assets/index'); },
  stopPullDownRefresh() {},
};
window.Page = value => { definition = value; };
function requireModule(key) {
  // 微信 Canvas 组件不在 DOM 中运行，但复用相同图表选项和 ECharts 引擎。
  if (key.endsWith('/components/ec-canvas/echarts.js') || key === 'components/ec-canvas/echarts.js') return window.echarts;
  if (cache[key]) return cache[key].exports;
  const source = manifest.modules[key];
  if (!source) throw new Error(`缺少模块 ${key}`);
  const module = { exports: {} }; cache[key] = module;
  const localRequire = relative => {
    const parts = key.split('/').slice(0, -1);
    for (const part of relative.split('/')) { if (part === '..') parts.pop(); else if (part !== '.') parts.push(part); }
    let resolved = parts.join('/'); if (!resolved.endsWith('.js')) resolved += '.js';
    return requireModule(resolved);
  };
  new Function('require', 'module', 'exports', source)(localRequire, module, module.exports);
  return module.exports;
}
function parseTemplate(source) {
  // HTML 不认识自闭合自定义标签，将其机械展开后再交给 DOMParser。
  source = source.replace(/<(image|ec-canvas|switch|textarea|template)\b([^>]*?)\/>/g, '<$1$2></$1>');
  // 容器使开头的 template 定义留在同一棵树，不被 HTML 解析器搬到 head 丢失。
  return new DOMParser().parseFromString(`<main>${source}</main>`, 'text/html').querySelector('main');
}
function renderChildren(source, parent, data, path) {
  let conditional = false;
  [...source.childNodes].forEach((node, index) => {
    let container = parent, visible = true;
    if (node.nodeType === Node.ELEMENT_NODE) {
      const isConditional = ['wx:if', 'wx:elif', 'wx:else'].some(name => node.hasAttribute(name));
      if (node.hasAttribute('wx:if')) { conditional = !!binding(node.getAttribute('wx:if'), data); visible = conditional; }
      else if (node.hasAttribute('wx:elif')) { visible = !conditional && !!binding(node.getAttribute('wx:elif'), data); conditional ||= visible; }
      else if (node.hasAttribute('wx:else')) { visible = !conditional; conditional = true; }
      else conditional = false;
      if (isConditional) {
        // 固定条件分支的位置。出现错误提示时只更新此处，不搬动后方的输入表单。
        container = document.createElement('div'); container.style.display = 'contents';
        container.dataset.focusKey = `${path}.${index}:condition`; parent.append(container);
      }
    }
    if (visible) renderNode(node, container, data, `${path}.${index}`);
  });
}
function renderNode(source, parent, data, path, inLoop = false) {
  if (source.nodeType === Node.TEXT_NODE) { parent.append(document.createTextNode(binding(source.textContent, data))); return; }
  if (source.nodeType !== Node.ELEMENT_NODE) return;
  if (source.hasAttribute('wx:for') && !inLoop) {
    const rows = binding(source.getAttribute('wx:for'), data) || [];
    const key = source.getAttribute('wx:key');
    rows.forEach((item, index) => {
      const identity = key === '*this' ? item : key ? item[key] : index;
      renderNode(source, parent, { ...data, [source.getAttribute('wx:for-item') || 'item']: item, [source.getAttribute('wx:for-index') || 'index']: index }, `${path}:${JSON.stringify(identity)}`, true);
    });
    return;
  }
  const attr = key => binding(source.getAttribute(key), data);
  const tag = source.tagName.toLowerCase();
  if (tag === 'template') {
    if (source.hasAttribute('name')) return;
    const named = [...active._template.querySelectorAll('template[name]')].find(t => t.getAttribute('name') === attr('is'));
    if (named) renderChildren(named.content, parent, templateData(source.getAttribute('data'), data), `${path}.template`);
    return;
  }
  let element;
  if (tag === 'ec-canvas') {
    const id = source.id;
    element = document.createElement('div');
    element.className = 'mini-chart'; element.id = id; parent.append(element); return;
  }
  if(tag==='switch'){
    element=document.createElement('input');element.type='checkbox';element.role='switch';element.checked=!!attr('checked');element.className='mini-switch';
  } else if (tag === 'picker') {
    // 原生 time picker 在浏览器映射为时间输入，不再误渲染为空的选项列表。
    const dateOrTime = ['date', 'time'].includes(attr('mode'));
    element = document.createElement(dateOrTime ? 'input' : 'select'); element.className = 'mini-picker';
    if (dateOrTime) { element.type = attr('mode'); element.value = attr('value') || ''; element.max = attr('end') || ''; }
    else { (attr('range') || []).forEach((item, index) => { const option = document.createElement('option'); option.value = index; option.textContent = attr('range-key') ? item[attr('range-key')] : String(item); element.append(option); }); element.value = attr('value'); }
  } else {
    element = document.createElement(({ view: 'div', text: 'span', image: 'img' })[tag] || tag);
    element.className = attr('class') || '';
    if (tag === 'image' || tag === 'img') { const src=attr('src');element.src = src?.startsWith('/assets/') ? `/mini-dist${src}` : src; element.alt = attr('aria-label') || ''; }
    else if (tag === 'input' || tag === 'textarea') {
      if(tag==='input')element.type = attr('password') ? 'password' : 'text';
      if (attr('type') === 'digit') element.inputMode = 'decimal';
      element.value = attr('value') ?? ''; element.placeholder = attr('placeholder') || '';
      if (attr('maxlength')) element.maxLength = Number(attr('maxlength'));
      element.setAttribute('aria-label', source.parentElement?.querySelector('.label')?.textContent || attr('placeholder') || '输入');
    } else renderChildren(source, element, data, path);
  }
  element.dataset.focusKey = path;
  if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
    element.oncompositionstart = e => { e.currentTarget._miniComposing = true; };
    element.oncompositionend = e => { e.currentTarget._miniComposing = false; };
  }
  if (attr('disabled')) element.disabled = true;
  if (attr('aria-label')) element.setAttribute('aria-label', attr('aria-label'));
  if (source.hasAttribute('aria-expanded')) element.setAttribute('aria-expanded', String(attr('aria-expanded')));
  const dataset = {};
  for (const attribute of source.attributes) if (attribute.name.startsWith('data-')) dataset[attribute.name.slice(5)] = binding(attribute.value, data);
  for (const [native, event] of [['bindtap', 'click'], ['bindinput', 'input'], ['bindchange', 'change']]) {
    if (!source.hasAttribute(native)) continue;
    const handler = source.getAttribute(native), page = active;
    element[`on${event}`] = e => {
      const target = e.currentTarget;
      e.stopPropagation(); if (target.disabled || active !== page) return;
      const value = tag==='switch' ? target.checked : target instanceof HTMLSelectElement ? Number(target.value) : target.value;
      const result = page[handler]({ detail: { value }, currentTarget: { dataset } });
      Promise.resolve(result).catch(error => wx.showToast({ title: error.message }));
    };
    if (native === 'bindtap' && tag !== 'button') { element.setAttribute('role', 'button'); element.tabIndex = 0; element.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.currentTarget.click(); } }; }
  }
  parent.append(element);
}
function render() {
  if (!active) return;
  const next = screen.cloneNode(false);
  renderChildren(active._template, next, active.data, 'root');
  patchView(screen, next, morphdom);
}
function navigate(url) {
  const path = url.split('?')[0].replace(/^\//, ''), query = Object.fromEntries(new URLSearchParams(url.split('?')[1] || ''));
  if (!manifest.app.pages.includes(path)) return;
  active?.onHide?.(); active?.onUnload?.();
  // 只在真正切页时清除旧 DOM；普通 setData 保留键盘、光标和已挂载的图表。
  screen.replaceChildren();
  currentPath = url; window.history.replaceState(null, '', `#${url}`);
  pageStyle.textContent = manifest.styles?.[path] || '';
  delete cache[`${path}.js`]; requireModule(`${path}.js`);
  active = { ...definition, data: clone(definition.data), _template: parseTemplate(manifest.templates[path]) };
  const page = active;
  page.setData = (patch, callback) => { Object.assign(page.data, patch); if (active === page) { render(); callback?.(); } };
  page.selectComponent = selector => ({ init(callback) {
    const node = screen.querySelector(selector); if (!node) return;
    node.setChart = () => {};
    return callback(node, node.clientWidth, node.clientHeight, window.devicePixelRatio);
  } });
  document.querySelector('#title').textContent = manifest.settings[path]?.navigationBarTitleText || 'FundScope';
  document.querySelector('#back').style.visibility = routeStack.length ? 'visible' : 'hidden';
  const tab = manifest.app.tabBar.list.find(t => t.pagePath === path);
  tabs.hidden = !tab; tabs.style.display = tab ? 'flex' : 'none'; tabs.replaceChildren();
  for (const item of manifest.app.tabBar.list) {
    const button = document.createElement('button'), image = document.createElement('img');
    image.src = `/mini-dist/${item === tab ? item.selectedIconPath : item.iconPath}`; image.alt = '';
    button.className = item === tab ? 'active' : ''; button.append(image, document.createTextNode(item.text));
    button.addEventListener('click', () => wx.switchTab({ url: `/${item.pagePath}` })); tabs.append(button);
  }
  render(); window.scrollTo(0, 0); page.onLoad?.(query); if (active === page) page.onShow?.();
}
document.querySelector('#back').addEventListener('click', () => wx.navigateBack());
navigate(location.hash.slice(1) || '/pages/assets/index');
