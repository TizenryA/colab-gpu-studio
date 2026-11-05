import { useState } from 'react';
import { Cpu, Layers, Monitor, Cloud, HardDrive, Leaf, Boxes, Sparkles, ChevronDown, Info, Square, Search } from 'lucide-react';
import type { ComputeSpec, GpuSettings, GpuUsage } from '../lib/gpu-api';
import { ComputeSpecs } from '../components/ComputeSpecs';
import { Card } from '../components/ui';
import { STRATEGIES } from '../lib/labels';
import styles from '../gpu.module.css';
import { cuNumber, shown } from '../lib/ui-helpers';

const STRATEGY_ICON = { save: Leaf, batch: Boxes, smart: Sparkles } as const;

export function ComputePage({
  settings, usage, onStop, onCheck, loadingStop, loadingCheck, specs = [], specsNote = '', error = '',
}: {
  settings: GpuSettings | null;
  usage: GpuUsage | null;
  onStop?: () => void;
  onCheck?: () => void;
  loadingStop?: boolean;
  loadingCheck?: boolean;
  specs?: ComputeSpec[];
  specsNote?: string;
  error?: string;
}) {
  const [open, setOpen] = useState<string>('');
  const realEnabled = Boolean(settings?.real_enabled) && settings?.offline_mode !== true;
  const activeCount = Number(usage?.active_runtimes ?? 0);
  const estimated = usage?.estimated_units;
  const actual = usage?.actual_units;
  const limit = usage?.daily_limit || settings?.budget?.daily_units;

  return (
    <div className={styles.stack}>
      <div className={styles.pageHead}>
        <h2><Cpu size={20} strokeWidth={1.6} aria-hidden />算力</h2>
        <p className="muted small">任务在后台运行，关掉手机也不会中断，回来再看就行。</p>
      </div>

      <Card icon={<Monitor size={17} strokeWidth={1.6} aria-hidden />} title="当前环境" aside={<span className={`pill ${realEnabled ? 'solid' : ''}`}>{realEnabled ? '真实 GPU' : '模拟模式'}</span>}>
        <p className="muted small">{realEnabled ? '任务会在 Google Colab 的 GPU 上真实运行。' : '现在是本地模拟，不会连接 Colab，也不会扣 CU。'}</p>
        <div className={styles.triple}>
          <div><small className="muted">真实 GPU</small><strong>{realEnabled ? '已启用' : '未启用'}</strong></div>
          <div><small className="muted">在线机器</small><strong>{activeCount}</strong></div>
          <div><small className="muted">文件存放</small><strong>{settings?.storage === 'drive' ? 'Google 云盘' : '本站服务器'}</strong></div>
        </div>
      </Card>

      <Card icon={<HardDrive size={17} strokeWidth={1.6} aria-hidden />} title="CU 用量">
        <div className={styles.grid2}>
          <div className={styles.statBox}><small className="muted">今日预估</small><strong>{cuNumber(estimated)}<em> CU</em></strong>{!realEnabled && <small className="muted">模拟估算</small>}</div>
          <div className={styles.statBox}><small className="muted">实际扣除</small><strong>{cuNumber(actual)}<em> CU</em></strong>{(actual === null || actual === undefined) && <small className="muted">暂无数据</small>}</div>
          <div className={styles.statBox}><small className="muted">每日上限</small><strong>{shown(limit)}<em> CU</em></strong></div>
          <div className={styles.statBox}><small className="muted">在线机器</small><strong>{activeCount}</strong></div>
        </div>
        <p className="muted small">这里的数字是站内按用时估算的，不是 Google 账单。实际余额以 Colab 官网为准。</p>
      </Card>

      <Card icon={<Layers size={17} strokeWidth={1.6} aria-hidden />} title="运行策略">
        <div className={styles.strategyList}>
          {STRATEGIES.map((s) => {
            const Icon = STRATEGY_ICON[s.value];
            const isOpen = open === s.value;
            return (
              <div key={s.value} className={styles.strategy} data-open={isOpen}>
                <button type="button" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? '' : s.value)}>
                  <Icon size={18} strokeWidth={1.5} aria-hidden />
                  <span><strong>{s.name}</strong><small className="muted">{s.short}</small></span>
                  <ChevronDown size={16} strokeWidth={1.6} aria-hidden className={styles.chev} />
                </button>
                {isOpen && <p className="muted small">{s.long}<br />在「创作 › 运行设置 › 运行策略」里选择。</p>}
              </div>
            );
          })}
        </div>
      </Card>

      <div className={styles.note}><Info size={16} strokeWidth={1.6} aria-hidden /><span>{realEnabled ? '用量数据来自站内记录，可能与 Google 账单略有出入。' : '模拟模式的数据仅供演示，不代表真实账单。'}</span></div>

      <div className={styles.twoButtons}>
        <button className="button" style={{ flex: 1 }} disabled={loadingCheck} type="button" onClick={onCheck}>
          <Search size={16} strokeWidth={1.8} aria-hidden />{loadingCheck ? '检查中…' : '检查可用 GPU'}
        </button>
        <button className="button danger" style={{ flex: 1 }} disabled={loadingStop} type="button" onClick={onStop}>
          <Square size={14} strokeWidth={1.8} aria-hidden />{loadingStop ? '停止中…' : '停止全部任务'}
        </button>
      </div>
      {specsNote && <p className="muted small" role="status">{specsNote}</p>}
      {specs.length > 0 && <ComputeSpecs specs={specs} />}
      {error && <p className="status error" role="alert">{error}</p>}
    </div>
  );
}

export function ThemePicker() {
  const [theme, setTheme] = useState(() => localStorage.getItem('studio-theme') || 'dark');
  function change(value: string) {
    setTheme(value);
    localStorage.setItem('studio-theme', value);
    document.documentElement.dataset.theme = value;
  }
  return (
    <div className="field">
      <label htmlFor="theme-select">界面颜色</label>
      <select id="theme-select" value={theme} onChange={(e) => change(e.target.value)}>
        <option value="dark">深色（黑底）</option>
        <option value="light">浅色（白底）</option>
        <option value="system">跟随手机系统</option>
      </select>
    </div>
  );
}
