/** tsc 负责编译业务代码；这里只做资源复制和可复现的图标/预览清单生成。 */
import { cp, mkdir, readdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';
import * as lucide from 'lucide';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const src = path.join(root, 'src'), dist = path.join(root, 'dist');
async function copyAssets(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const source = path.join(dir, entry.name);
    if (entry.isDirectory()) await copyAssets(source);
    else if (!entry.name.endsWith('.ts')) {
      const target = path.join(dist, path.relative(src, source));
      await mkdir(path.dirname(target), { recursive: true }); await cp(source, target);
    }
  }
}
await copyAssets(src);
// .wxss 在 HTTP 静态托管下不是 CSS MIME 类型，预览另存一个同内容 .css 文件。
await cp(path.join(dist, 'app.wxss'), path.join(dist, 'app.css'));
// 只用于浏览器预览的 DOM 更新器，构建时本地复制，不依赖手机访问 CDN。
await mkdir(path.join(dist, 'preview-vendor'), { recursive: true });
await cp(path.join(root, 'node_modules/morphdom/dist/morphdom-esm.js'), path.join(dist, 'preview-vendor/morphdom.mjs'));
await cp(path.join(root, 'node_modules/morphdom/LICENSE'), path.join(dist, 'preview-vendor/MORPHDOM-LICENSE'));
// 图标来自 Lucide 的节点数据，机械转成微信 tabBar 支持的 PNG，不手绘图标。
const icons = { wallet: 'Wallet', chart: 'ChartNoAxesCombined', user: 'UserRound', refresh: 'RefreshCw', plus: 'Plus', trash: 'Trash2', chevron: 'ChevronRight', back: 'ArrowLeft', search: 'Search', news: 'Newspaper', community: 'MessagesSquare', radar: 'Radar', sparkles: 'Sparkles', settings: 'Settings2', send: 'ArrowUp', stop: 'Square', copy: 'Copy', shield: 'ShieldAlert' };
function serialize(node) {
  const [tag, attrs, children = []] = node;
  return `<${tag} ${Object.entries(attrs).map(([key, value]) => `${key}="${String(value)}"`).join(' ')}>${children.map(serialize).join('')}</${tag}>`;
}
await mkdir(path.join(dist, 'assets'), { recursive: true });
for (const [name, symbol] of Object.entries(icons)) {
  for (const active of [false, true]) {
    const icon = lucide[symbol];
    if (!icon) throw new Error(`Unknown Lucide icon: ${symbol}`);
    const svg = serialize(['svg', { ...icon[1], width: 48, height: 48, stroke: active ? '#edc85b' : '#b0b1bb', 'stroke-width': 1.8 }, icon[2]]);
    await sharp(Buffer.from(svg)).png().toFile(path.join(dist, 'assets', `${name}${active ? '-active' : ''}.png`));
  }
}
// 预览读取同一套 Page/WXML。排除微信专用 Canvas 引擎，由浏览器 ECharts 适配。
const app = JSON.parse(await readFile(path.join(dist, 'app.json'), 'utf8'));
const manifest = { app, modules: {}, templates: {}, settings: {}, styles: {} };
for (const folder of ['lib', 'pages']) {
  async function collect(dir) {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      const file = path.join(dir, entry.name), key = path.relative(dist, file).replaceAll(path.sep, '/');
      if (entry.isDirectory()) await collect(file);
      else if (entry.name.endsWith('.js')) manifest.modules[key] = await readFile(file, 'utf8');
      else if (entry.name.endsWith('.wxml')) manifest.templates[key.replace(/\.wxml$/, '')] = await readFile(file, 'utf8');
      else if (entry.name.endsWith('.json')) manifest.settings[key.replace(/\.json$/, '')] = JSON.parse(await readFile(file, 'utf8'));
      // 原生会自动加载页面 WXSS；浏览器预览也要随路由切换同一份样式。
      else if (entry.name.endsWith('.wxss')) manifest.styles[key.replace(/\.wxss$/, '')] = await readFile(file, 'utf8');
    }
  }
  await collect(path.join(dist, folder));
}
await writeFile(path.join(dist, 'preview-manifest.json'), JSON.stringify(manifest));
console.log(`Built ${app.pages.length} native pages and browser preview.`);
