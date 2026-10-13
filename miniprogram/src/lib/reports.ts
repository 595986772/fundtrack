/** 将服务端报告变成手机可读段落。只格式化，不在前端重新计算金融结论。 */
import { number, chinaTime } from './format';
export interface Block { key: string; kind: 'heading' | 'text' | 'metric' | 'link'; label: string; text: string; url?: string; depth: number }
// 明确的业务字段词典，避免把 JSON、工具内部参数或英文调试字段堆给用户。
const labels: Record<string, string> = {
  summary:'摘要', headline:'核心结论', heading:'要点', overview:'综合观点', detail:'说明', explanation:'解释', rationale:'判断理由',
  title:'标题', label:'名称', short_name:'基金', fund_code:'基金代码', portfolio_name:'账本', name:'名称',
  state_label:'当前状态', status:'状态', confidence:'置信度', confidence_reason:'可信度说明',
  value_text:'数值', data_date:'数据日期', report_date:'披露日期', assessment_date:'研判日期',
  published_at:'发布时间', published_date:'发布日期', event_date:'事件日期', created_at:'创建时间', generated_at:'生成时间',
  source_name:'来源', source:'来源', source_url:'来源链接', methodology_url:'计算方法原文',
  evidence_id:'证据编号', evidence_ids:'引用证据', post_ids:'引用帖子', references:'证据引用',
  content_excerpt:'正文', action_label:'作者自述操作', position_text:'仓位表述', analysis_status:'分析状态',
  themes:'主题', tags:'标签', uncertainties:'不确定性', limitations:'局限', disclaimer:'风险提示', methodology:'计算口径',
  missing_information:'信息缺口', warnings:'注意事项', assumptions:'计算假设',
  findings:'主要发现', disagreements:'分歧', key_findings:'关键发现', risk_evidence:'风险证据', supporting_evidence:'支撑证据', neutral_evidence:'中性证据',
  model_interpretation:'AI 研判', conditional_view:'条件式观点', conflicts:'相互冲突的证据', follow_up_items:'待跟进事项',
  opportunity:'机会', risks:'风险', opportunities:'机会', evidence:'依据', sources:'来源', data_sources:'数据来源',
  source_urls:'来源链接', risk_drivers:'风险来源', reason:'原因', tracking_reason:'追踪理由', impact_reason:'影响路径',
  semantic_analysis:'AI 事件分析', impact_direction:'影响方向', freshness_status:'时效', valid_until:'有效期至',
  source_level:'来源等级', source_grade_reason:'来源说明', event_type:'事件类型', age_days:'距事件天数',
  heat_score:'热度', lifecycle_label:'热度阶段', heat_change_24h:'24小时热度变化', heat_change_3d:'3日热度变化',
  author_name:'作者', author_count:'作者数', sample_count:'样本数', total_sample_count:'总样本数',
  accepted_sample_count:'纳入操作统计', action_counts:'操作分布', positioning_label:'观点倾向', disagreement_ratio:'分歧比例',
  exclusion_reasons:'未纳入操作统计的原因', model_name:'模型', analyzed_at:'分析时间', ocr_text:'图片识别文字',
  images:'图片证据', text:'内容', analysis:'解读', extraction_status:'识别状态', image_type:'图片类型',
  performance_label:'业绩口径', period_start:'区间开始', period_end:'区间结束', observation_count:'观察日数',
  twr_pct:'时间加权收益', xirr_pct:'资金加权年化收益', annualized_return_pct:'年化收益', annualized_volatility_pct:'年化波动',
  max_drawdown:'最大回撤', max_drawdown_pct:'最大回撤', drawdown_pct:'回撤', peak_date:'高点日期', trough_date:'低点日期', recovery_date:'修复日期',
  sharpe_ratio:'夏普比率', sortino_ratio:'Sortino 比率', risk_free_rate_annual_pct:'年化无风险利率', risk_free_rate_source:'无风险利率来源',
  benchmark:'比较基准', benchmark_name:'基准名称', period_return_pct:'区间收益', excess_return_pct:'超额收益',
  cash_flows:'资金流', opening_value:'期初价值', contributions:'投入', withdrawals:'取出', fees:'费用', ending_value:'期末价值',
  attribution:'收益贡献', pnl_contribution_amount:'盈亏贡献金额', return_contribution_pct_points:'收益贡献百分点', ending_weight_pct:'期末占比',
  concentration:'集中度', max_weight_pct:'最大单只占比', effective_fund_count:'有效基金数', allocations:'资产分布', weight_pct:'占比',
  fund_contributions:'基金贡献', score_breakdown:'规则评分', supportive_points:'支撑分', caution_points:'风险分', net_points:'净分',
  holdings:'持仓', stocks:'股票持仓', bonds:'债券持仓', industries:'行业分布', security_name:'证券名称', security_code:'证券代码',
  stock_holdings:'股票持仓', bond_holdings:'债券持仓', industry_allocations:'行业分布', industry_name:'行业', market_value:'市值',
  top10_concentration_pct:'前十集中度', coverage_pct:'覆盖率', known_exposure_pct:'已知暴露', disclosure_date:'披露日期',
  quality:'数据质量', top_holdings:'主要持仓', exposure:'底层暴露', sector_exposures:'板块暴露', impacts:'持仓事件影响',
  affected_exposure_pct:'影响仓位', match_basis:'关联依据', matched_funds:'关联基金',
  topic_label:'板块', data_cutoff:'数据截止', net_flow_cny:'净申购估算', flow_1d_cny:'近1日净申购估算', flow_5d_cny:'近5日净申购估算', flow_20d_cny:'近20日净申购估算',
  etf_count:'ETF样本数', coverage_5d_pct:'5日覆盖率', interpretation:'解读', boundary:'边界', identity_boundary:'机构身份边界',
  explanation_label:'说明', confirmed_national_team_disclosure_count:'有披露佐证的国家队记录数',
  institutional_signal:'机构公开线索', provider_main_net_inflow_cny:'数据源主力净流入', topic_signals:'主题资金',
  indicators:'情绪参考', value:'当前数值', unit:'单位', returns:'区间变化', value_pct:'涨跌幅', start_date:'起始日期', end_date:'截至日期', last_error:'数据状态说明',
  thesis:'决策理由', action:'决策动作', decision_date:'决策日期', target_date:'观察到期', horizon_days:'观察天数', outcome:'后续表现', review_note:'复盘笔记', note:'备注',
  policy:'跟踪策略', latest_snapshot:'最近风险快照', alerts:'预警', severity:'程度', open_alert_count:'未读预警',
  enabled:'已启用', risk_preference:'风险偏好', investment_horizon:'投资期限',
  scenarios:'情景比较', window:'压力区间', facts:'计算事实', worst_daily_return_pct:'最差单日收益', weights:'配置权重',
  contributions_pct_points:'贡献百分点', metrics:'指标', period_months:'样本月数', months:'样本月数',
  proposal:'待确认条件', questions:'待澄清问题', unsupported_constraints:'暂不支持的条件', classifications:'基金分类', fund_type:'基金类型', is_bond:'是否债券类基金',
  min_bond_weight_pct:'债券类最低占比', max_single_fund_weight_pct:'单只最高占比', max_turnover_pct:'单边换手上限',
  result:'求解结果', diagnostics:'不可行诊断', conflicts_found:'冲突条件', constraints:'确认的条件', candidate_weights:'候选权重',
  stress_comparison:'同情景比较', candidate:'候选配置', baseline:'原始配置', turnover_pct:'单边换手',
  conditional_outlook:'条件式前景', counterarguments:'反对理由', research_snapshot:'冻结证据', snapshot_id:'快照编号', captured_at:'冻结时间',
  generation:'模型运行', model_used:'是否使用模型', model:'模型', fallback_reason:'回退原因',
  quant_risk_snapshot:'量化风险', performance_snapshot:'历史业绩', decision_history:'决策历史',
  assessment_snapshot:'规则评估', market_snapshot:'市场证据', risk_contributions:'波动风险贡献',
  risk_share_pct:'波动风险占比（带符号）', absolute_risk_share_pct:'绝对波动风险占比', annualized_risk_contribution_pct_points:'年化波动贡献（百分点）',
  top_risk_contributor:'最大风险贡献基金', correlation:'相关性', strongest_pair:'最强相关组合', left_fund_code:'基金一', right_fund_code:'基金二',
  price_observation_count:'净值样本数', return_observation_count:'收益样本数', snapshot_end:'样本截止',
  question:'需求', sample:'历史样本', eligible_count:'有效样本', excluded_count:'排除样本', status_reason:'状态说明',
  sector_rows:'热门板块统计', direction_label:'资金方向', trade_date_start:'统计开始', trade_date_end:'统计结束',
  buy_cny:'机构席位买入额', sell_cny:'机构席位卖出额', net_buy_cny:'机构席位净买入', stock_count:'证券样本数',
  disclosure:'披露档案', security_holdings:'证券持仓', asset_evidence:'披露资产小计', security_coverage_pct:'证券覆盖率',
  gross_disclosed_weight_pct:'披露权重合计', unresolved_weight_pct:'未穿透占比', comparisons:'与前期披露比较', rows:'明细',
  latest_report_date:'最新披露日期', report_age_days:'距披露天数', disclosed_stock_weight_pct:'已披露股票占比', disclosed_bond_weight_pct:'已披露债券占比',
  top_stock_weight_pct:'最大股票占比', top_five_disclosed_stocks_pct:'前五股票占比', top_industry_name:'最大行业', top_industry_weight_pct:'最大行业占比',
  previous_weight_pct:'上期权重', previous_report_date:'上期披露', change_pp:'变化（百分点）',
  risk_adjusted:'风险调整后指标', benchmark_comparison:'基准比较', current_drawdown_pct:'当前回撤', downside_volatility_pct:'下行波动',
  return_basis_label:'收益口径', return_basis_status:'收益口径状态', data_quality_warnings:'数据质量提醒',
  rule_baseline:'规则基线', model_interpretation_status:'模型状态', event_judgments:'逐事件研判', effective_overall_view:'综合倾向', overall_confidence:'整体置信度',
  trend:'市场趋势', volatility:'波动', breadth:'市场广度', activity:'交易活跃度', signals:'信号',
  advancing_count:'上涨家数', declining_count:'下跌家数', advance_ratio_pct:'上涨比例', latest_amount_cny:'最新成交额',
  return_30d_pct:'近30日涨跌', return_3m_pct:'近3个月涨跌',
  recognition:'图片识别', full_text:'图片文字', image_count:'图片数',
  access_message:'采集状态说明', collection_allowed:'允许采集', verification_required:'需要平台验证', session_ready:'登录会话就绪', login_required:'需要登录',
  next_collection_at:'下次可采集时间', tracked_author_count:'关注作者数', keyword_count:'关键词数', post_count:'已保存帖子', analyzed_count:'已分析帖子',
  operation_window:'有效操作窗口', target_trade_date:'目标交易日', window_start:'窗口开始', window_end:'窗口结束', cutoff_at:'操作截止',
  checks:'约束校验', actual_pct:'实际值', limit_pct:'约束值', passed:'是否满足',
  target_weights:'候选权重', cvar_95_daily_pct:'历史单日95% CVaR', mean_daily_return_pct:'平均日收益', risk_improved:'历史尾部风险是否改善', stress_report:'压力比较',
  input_hash:'冻结输入指纹', draft_hash:'草案指纹', engine_version:'计算版本', solver_version:'求解版本',
  range_start:'区间开始', range_end:'区间结束', error:'读取异常',
  research_report:'研究报告', financial_research_report:'金融研究报告', investment_context:'投资背景', evidence_ledger:'证据清单',
  tool_calls:'工具调用', tool_name:'工具', is_error:'是否失败', stop_reason:'结束原因',
  interrupt:'待确认事项', portfolio_research_jobs:'组合研究任务', portfolio_stress_runs:'压力报告', portfolio_allocation_drafts:'配置草案',
  adjustments:'调整明细', proposed_weights:'建议权重', rationale_text:'调整依据', scenario_validation:'情景校验', validation:'校验结果',
  user_constraints:'用户约束', baseline_weights:'原权重', investment_thesis:'投资逻辑', changes:'变化', before_text:'原值', after_text:'现值',
  drivers:'变化原因', tone:'证据方向', main_risks:'主要风险', main_opportunities:'主要机会', recommendation:'建议',
  recommendation_reason:'建议理由', action_plan:'行动方案', answer_check:'回答核验', warnings_count:'提醒数量',
  current_weight_pct:'当前权重', target_weight_pct:'候选权重', change_pct_points:'调整（百分点）',
  key_tradeoffs:'主要取舍', monitoring_items:'后续观察', tradeoffs:'取舍', stance:'方案倾向',
  review_request:'待确认研究背景', required_constraints:'需要确认的约束', source_headline:'依据的研究结论',
  daily_cvar_95_pct:'历史单日95% CVaR', concentration_hhi:'集中度 HHI', constraints_passed:'约束是否满足',
  source_snapshot:'冻结样本', baseline_consistency:'基线复算一致性', consistent:'是否一致', information_gaps:'信息缺口',
  base_run:'上次研究', target_run:'本次研究', input_changed:'输入是否变化', conclusion_changed:'结论是否变化',
  changed_sections:'发生变化的证据分区', changed:'是否变化', delta_text:'变化', fetch_errors:'来源异常',
};
const states: Record<string, string> = { supportive:'偏支持', caution:'需谨慎', neutral:'中性', positive:'偏正面', negative:'偏负面', mixed:'分化', uncertain:'不确定',
  high:'高', medium:'中', low:'低', pending:'待处理', accepted:'已采纳', abstained:'证据不足', available:'可用', unavailable:'暂不可用',
  fresh:'时效内', decaying:'时效衰减', expired:'已过期', ready:'已生成', stale:'已过期或有更新', not_generated:'尚未生成', empty:'暂无样本',
  buy:'买入', add:'加仓', reduce:'减仓', sell:'卖出', hold:'持有', watch:'观察', unknown:'未明确', rebalance:'调整配置',
  awaiting_confirmation:'待确认', solved:'已求解', infeasible:'条件无解', open:'未读', acknowledged:'已读',
  tracking:'观察中', review_ready:'待复盘', reviewed:'已复盘', policy:'政策', economic_data:'经济数据', regulation:'监管', fund_announcement:'基金公告', market_notice:'市场消息', social_positioning:'社区观点' };
export function stateLabel(value: unknown) { const s = String(value ?? '--'); return states[s] || s; }
export function reportBlocks(data: unknown): Block[] {
  const blocks: Block[] = [];
  // 后端报告同时提供顶层摘要和模型审计原文。相同的叙述只显示一次，指标不能据此去重。
  const narratives=new Set(['headline','summary','key_findings','findings','conflicts','uncertainties','follow_up_items']);
  const seenNarratives=new Set<string>();
  function visit(value: any, key: string, depth: number, path: string, caption?: string) {
    const label = caption || labels[key];
    if (!label || depth > 9) return;
    if(!caption&&narratives.has(key)&&value!=null){const signature=key+':'+JSON.stringify(value);if(seenNarratives.has(signature))return;seenNarratives.add(signature);}
    if (value === null || value === undefined || typeof value !== 'object') {
      const integer=key.endsWith('_count')||['months','period_months','age_days','horizon_days','report_age_days'].includes(key);
      const text = value == null ? '--' : typeof value === 'boolean' ? value ? '是' : '否' : typeof value === 'number' ? `${number(value,integer?0:2)}${key.endsWith('_pct') ? '%' : ''}` : key.endsWith('_at') ? chinaTime(value) : stateLabel(value);
      const url = typeof value === 'string' && /^https?:\/\/[^\s]+$/.test(value) ? value : undefined;
      blocks.push({ key:path, kind:url ? 'link' : typeof value === 'number' ? 'metric' : 'text', label, text, url, depth }); return;
    }
    if (Array.isArray(value) && !value.length) return;
    blocks.push({ key:path, kind:'heading', label, text:'', depth });
    if (Array.isArray(value)) value.forEach((row, i) => visit(row, key, depth + 1, `${path}.${i}`, typeof row === 'object' && row ? row.title || row.label || row.short_name || row.name || `${label} ${i + 1}` : label));
    else for (const [child, item] of Object.entries(value)) visit(item, child, depth + 1, `${path}.${child}`, /^\d{6}$/.test(child) ? `基金 ${child} (%)` : undefined);
  }
  if (Array.isArray(data)) visit(data, 'result', 0, 'report');
  else if (data && typeof data === 'object') for (const [key, value] of Object.entries(data)) visit(value, key, 0, key);
  return blocks;
}
