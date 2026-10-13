/**
 * 浏览器预览专用。原生微信 setData 会保留输入控件，预览也应有同样的行为。
 * 用成熟的 morphdom 原位更新，不能每打一个字就移除输入框，再用 focus() 补救。
 */
const EVENTS = ['onclick', 'oninput', 'onchange', 'onkeydown', 'oncompositionstart', 'oncompositionend'];

export function patchView(root, next, morphdom) {
  morphdom(root, next, {
    childrenOnly: true,
    // WXML 循环的业务 key 参与定位，删除前一行时不会把光标移到另一只基金。
    getNodeKey: node => node.dataset?.focusKey || node.id,
    onBeforeElUpdated(current, target) {
      // 保留节点，但同步最新事件闭包：循环索引和当前数据可能已经变化。
      for (const event of EVENTS) current[event] = target[event];
      // 图表内部 DOM 由 ECharts 管，不被空的模板占位符清除或搬离文档。
      if (current.classList.contains('mini-chart') && current.id === target.id) return false;
      // 中文拼音组词期间不重写 value，避免破坏正在输入的候选文本。
      if (current._miniComposing) return false;
      return true;
    },
  });
}
