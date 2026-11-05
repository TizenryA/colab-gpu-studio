import { useMemo, useRef } from 'react';
import type { GalleryAsset, GpuManifest, GpuParameter } from '../lib/gpu-api';
import { friendlyHint, friendlyLabel, MODE_HELP, modeHelp } from '../lib/labels';
import { InfoTip } from './ui';
import styles from '../gpu.module.css';

export type ParamSection = 'assets' | 'options' | 'advanced';

type Props = {
  manifest?: GpuManifest | null;
  values: Record<string, unknown>;
  assets: GalleryAsset[];
  uploadProgress: Record<string, number>;
  onChange: (key: string, value: unknown) => void;
  onUpload: (file: File) => Promise<void>;
  /** 已由别处处理的字段（提示词、宽高），这里不再渲染，避免重复。 */
  excludeKeys?: string[];
  /** assets=素材文件；options=常用选项（时长/模式等，直接展示）；advanced=其余全部（默认折叠）。 */
  section: ParamSection;
};

type Entry = { param: GpuParameter; key: string };

export function parametersOf(manifest?: GpuManifest | null): GpuParameter[] {
  const raw = manifest?.parameters;
  if (Array.isArray(raw)) return raw;
  if (!raw || typeof raw !== 'object') return [];
  return Object.entries(raw).map(([key, parameter]) => {
    const normalized = parameter as GpuParameter;
    return { ...normalized, key, id: normalized.id || key };
  });
}

function keyOf(parameter: GpuParameter, index: number) {
  return parameter.key || parameter.id || parameter.name || `parameter_${index + 1}`;
}

function optionOf(option: unknown): { value: string; label: string } {
  if (typeof option === 'object' && option !== null && 'value' in option) {
    return {
      value: String(option.value),
      label: 'label' in option && option.label ? String(option.label) : String(option.value),
    };
  }
  return { value: String(option), label: String(option) };
}

function initialValue(parameter: GpuParameter): unknown {
  if (parameter.default !== undefined) return parameter.default;
  if (parameter.widget === 'checkbox' || parameter.type === 'boolean') return false;
  if (parameter.type === 'integer' || parameter.type === 'number' || parameter.widget === 'number') return parameter.min ?? 0;
  return '';
}

function widgetOf(parameter: GpuParameter): string {
  const hasOptions = Boolean(parameter.options?.length);
  return (
    parameter.widget ||
    (parameter.type === 'text' ? 'textarea'
      : parameter.type === 'boolean' ? 'checkbox'
      : parameter.type === 'integer' || parameter.type === 'number' ? 'number'
      : ['image', 'video', 'file'].includes(parameter.type) ? 'file'
      : hasOptions ? 'select'
      : 'text')
  );
}

const TECH_KEYS = /(^|_)(steps?|seed|cfg|cfg_scale|sampler|sampler_name|scheduler|denoise|clip_skip|negative|strength|weight)(_|$)/i;
const COMMON_KEYS = /(^|_)(mode|duration|seconds|length|quality|fps)(_|$)/i;

/** 把参数分成三层：素材、常用选项、高级。常用选项只放“模式 / 时长”这类会明显改变结果的下拉项。 */
export function splitParams(manifest: GpuManifest | null | undefined, excludeKeys: string[] = []) {
  const assets: Entry[] = [];
  const options: Entry[] = [];
  const advanced: Entry[] = [];
  parametersOf(manifest).forEach((param, index) => {
    const key = keyOf(param, index);
    if (excludeKeys.includes(key)) return;
    const widget = widgetOf(param);
    const flaggedAdvanced = Boolean(param.advanced) || param.group === 'advanced';
    if (widget === 'file') assets.push({ param, key });
    else if (!flaggedAdvanced && !TECH_KEYS.test(key) && COMMON_KEYS.test(key) && (widget === 'select' || widget === 'number')) options.push({ param, key });
    else advanced.push({ param, key });
  });
  return { assets, options, advanced };
}

export function GpuParamForm({ manifest, values, assets, uploadProgress, onChange, onUpload, excludeKeys = [], section }: Props) {
  const inputRefs = useRef<Record<string, HTMLInputElement | null>>({});
  const entries = useMemo(() => splitParams(manifest, excludeKeys)[section], [manifest, excludeKeys, section]);
  if (!entries.length) return null;

  function renderField({ param: parameter, key }: Entry) {
    const label = friendlyLabel(key, parameter.label || parameter.name || key);
    const current = values[key] === undefined ? initialValue(parameter) : values[key];
    const options = (parameter.options || []).map(optionOf);
    const widget = widgetOf(parameter);
    const hasRange = parameter.min !== undefined || parameter.max !== undefined;
    const hint = parameter.description || friendlyHint(key);
    const isMode = /(^|_)mode$/i.test(key);
    const selectedHelp = isMode ? modeHelp(current) : undefined;

    return (
      <div className={styles.paramField} key={key}>
        <div className={styles.paramLabel}>
          <label htmlFor={`gpu-${key}`}>
            {label}
            {parameter.required && <b className={styles.requiredMark} aria-label="必填">*</b>}
          </label>
          <span className={styles.paramLabelRight}>
            {hasRange && <small className="muted">范围 {parameter.min ?? 0} ~ {parameter.max ?? '∞'}</small>}
            {isMode && options.some((o) => MODE_HELP[o.value.toLowerCase()]) && (
              <InfoTip
                label={`${label}说明`}
                text={<>{options.filter((o) => MODE_HELP[o.value.toLowerCase()]).map((o) => (
                  <span key={o.value} className={styles.tipLine}><b>{MODE_HELP[o.value.toLowerCase()].name}</b>：{MODE_HELP[o.value.toLowerCase()].text}</span>
                ))}</>}
              />
            )}
          </span>
        </div>

        {widget === 'textarea' ? (
          <textarea id={`gpu-${key}`} value={String(current ?? '')} rows={2} onChange={(e) => onChange(key, e.target.value)} />
        ) : widget === 'select' ? (
          <select id={`gpu-${key}`} value={String(current ?? '')} onChange={(e) => onChange(key, e.target.value)}>
            {!options.some((o) => o.value === String(current ?? '')) && <option value="">请选择</option>}
            {options.map((option) => (
              <option key={option.value} value={option.value}>{modeHelp(option.value) && isMode ? `${modeHelp(option.value)!.name}（${option.value}）` : option.label}</option>
            ))}
          </select>
        ) : widget === 'checkbox' ? (
          <label className={styles.checkRow}>
            <input id={`gpu-${key}`} type="checkbox" checked={Boolean(current)} onChange={(e) => onChange(key, e.target.checked)} />
            <span>开启</span>
          </label>
        ) : widget === 'number' ? (
          <input
            id={`gpu-${key}`}
            type="number"
            inputMode="decimal"
            value={String(current ?? '')}
            min={parameter.min}
            max={parameter.max}
            step={parameter.step || (parameter.type === 'integer' ? 1 : 'any')}
            onChange={(e) => onChange(key, e.target.value === '' ? '' : parameter.type === 'integer' ? Number.parseInt(e.target.value, 10) : Number(e.target.value))}
          />
        ) : widget === 'file' ? (
          <div className={styles.fileControl}>
            <select id={`gpu-${key}`} value={String(current ?? '')} onChange={(e) => onChange(key, e.target.value)}>
              <option value="">从素材库选择…</option>
              {assets
                .filter((asset) => !parameter.type || parameter.type === 'file' || asset.mime.startsWith(`${parameter.type}/`))
                .map((asset) => <option key={asset.id} value={asset.id}>{asset.filename || asset.id}</option>)}
            </select>
            <button type="button" className="button secondary" onClick={() => inputRefs.current[key]?.click()}>上传</button>
            <input
              ref={(node) => { inputRefs.current[key] = node; }}
              type="file"
              hidden
              accept={parameter.type === 'image' ? 'image/*' : parameter.type === 'video' ? 'video/*' : undefined}
              onChange={async (event) => {
                const file = event.target.files?.[0];
                if (file) await onUpload(file);
                event.target.value = '';
              }}
            />
            {Object.entries(uploadProgress).map(([name, progress]) => (
              <small className={styles.hint} key={name}>正在上传 {name}：{progress}%</small>
            ))}
          </div>
        ) : (
          <input id={`gpu-${key}`} type="text" value={String(current ?? '')} onChange={(e) => onChange(key, e.target.value)} />
        )}

        {selectedHelp ? <span className={styles.hint}>{selectedHelp.text}</span> : hint ? <span className={styles.hint}>{hint}</span> : null}
      </div>
    );
  }

  return <div className={styles.paramList}>{entries.map(renderField)}</div>;
}
