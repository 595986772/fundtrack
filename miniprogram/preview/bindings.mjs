/** 浏览器预览专用的模板绑定。不进入微信包，也不接受远程提供的模板代码。 */
const expressions = new Map();
function expression(source, data) {
  if (!expressions.has(source)) expressions.set(source, new Function('data', `with(data) { return (${source}); }`));
  return expressions.get(source)(data);
}
export function binding(value, data) {
  if (value == null) return '';
  // 单段插值保留数组/布尔值类型；多段插值合成文本，避免将 }} / {{ 当作代码。
  const exact = value.match(/^{{([^{}]*?)}}$/);
  if (exact) return expression(exact[1], data);
  return value.replace(/{{([\s\S]*?)}}/g, (_, code) => expression(code, data) ?? '');
}
// 原生 template 的 data 是对象展开/简写语法，只解析本地 WXML 中的表达式。
export function templateData(value, data) {
  return expression(`{${value.slice(2,-2)}}`, data);
}
