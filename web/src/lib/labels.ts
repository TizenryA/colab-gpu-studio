/** 把工作流里的技术字段名翻译成普通人能看懂的说明（只影响显示，不改变提交的参数）。 */

type Rule = { test: RegExp; label: string; hint: string };

const RULES: Rule[] = [
  { test: /negative/i, label: '负向提示词', hint: '写下不想出现的内容，例如「模糊、多余的手指」。' },
  { test: /(^|_)seed$/i, label: '随机种子', hint: '种子和参数都相同，结果就相近；填 -1 表示每次随机。' },
  { test: /(^|_)steps?$/i, label: '采样步数', hint: '步数越多细节越好，但更慢、更费 CU。' },
  { test: /(^|_)cfg(_scale)?$/i, label: '提示词相关度（CFG）', hint: '越高越贴近提示词，太高画面会发硬。拿不准就保持默认。' },
  { test: /sampler(_name)?$/i, label: '采样器', hint: '出图的算法，拿不准就保持默认。' },
  { test: /scheduler$/i, label: '调度器', hint: '配合采样器使用，拿不准就保持默认。' },
  { test: /denoise$/i, label: '重绘强度', hint: '越高改动越大，越低越接近原图。' },
  { test: /(^|_)(duration|seconds|length)$/i, label: '视频时长（秒）', hint: '时间越长越费 CU、越慢。' },
  { test: /(^|_)fps$/i, label: '帧率', hint: '每秒多少帧，越高越流畅。' },
  { test: /(lora).*(strength|weight)|(strength|weight).*lora/i, label: 'LoRA 强度', hint: '越大风格越明显；0 表示不用。' },
  { test: /(^|_)mode$/i, label: '运行模式', hint: '' },
];

const LOOKS_LIKE_ID = /^[a-z0-9_.\- ]+$/i;

export function friendlyLabel(key: string, label: string): string {
  if (label && !LOOKS_LIKE_ID.test(label)) return label;
  const hit = RULES.find((rule) => rule.test.test(key));
  return hit ? hit.label : label || key;
}

export function friendlyHint(key: string): string {
  return RULES.find((rule) => rule.test.test(key))?.hint || '';
}

/** 常见“模式”选项的解释（按选项值匹配；匹配不到就不显示）。 */
export const MODE_HELP: Record<string, { name: string; text: string }> = {
  fast: { name: '快速', text: '少步数快速出结果，最省时间和 CU，适合先试效果。' },
  hq: { name: '高质量', text: '步数更多，画面更细腻，但更慢、更费 CU。' },
  refine: { name: '精修', text: '先快速出一版，再放大并重画细节，更清晰，耗时介于两者之间。' },
  refine8: { name: '精修（8 步）', text: '精修的加强版，细节更好，但更慢。' },
};

export function modeHelp(value: unknown): { name: string; text: string } | undefined {
  return MODE_HELP[String(value).toLowerCase()];
}

export const STRATEGIES = [
  { value: 'save', name: '节省', short: '跑完就关机', long: '队列里的任务跑完立刻释放 GPU，不留空闲时间。最省 CU，下次要重新启动机器。' },
  { value: 'batch', name: '批次', short: '同批任务连着跑', long: '同一批里兼容的任务共用一台机器，一个接一个跑，省掉重复启动的时间，跑完照样释放。' },
  { value: 'smart', name: '智能', short: '跑完先等一会', long: '任务跑完后机器再保留约 2 分钟，期间连续提交新任务会更快；没有新任务也会白耗一点 CU。' },
] as const;

export const RUNTIME_HELP = {
  mock: '模拟运行：不连接 Google，不扣 CU，只用来试界面和流程，出的是示例图。',
  colab: '真实运行：在 Google Colab 的 GPU 上出图，会按用时扣 CU。',
};
