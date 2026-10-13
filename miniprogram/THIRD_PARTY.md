# Third-Party Components

- ECharts for Weixin: https://github.com/ecomfe/echarts-for-weixin
  - Pinned commit: `82e6cdfe5347265c04cced222bd6398dcb6eb279`.
  - `src/components/ec-canvas/` is an unmodified copy of the upstream component.
  - Apache-2.0 license retained in that directory; bundled ECharts retains its license header.
- Lucide 0.468.0: https://github.com/lucide-icons/lucide (ISC).
  - PNG icons are generated from the dependency's icon nodes at build time.
- Browser preview reuses the project's existing ECharts 6.0.0 distribution.
- Morphdom 2.7.8: https://github.com/patrick-steele-idem/morphdom (MIT).
  - Used only by the browser preview for in-place DOM updates and stable form focus.
  - Build copies its license alongside the module; both are excluded from the WeChat package.

Third-party source is not rewritten to add tutorial comments. Learning comments live in the FundScope page that calls the component.
