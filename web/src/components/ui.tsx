import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Info } from 'lucide-react';
import styles from '../gpu.module.css';

/** 卡片：左上线性图标 + 标题，右侧可放徽标或按钮。 */
export function Card({ icon, title, aside, children, className }: {
  icon?: ReactNode; title?: ReactNode; aside?: ReactNode; children?: ReactNode; className?: string;
}) {
  return (
    <section className={`${styles.card} ${className || ''}`}>
      {(title || aside) && (
        <div className={styles.cardHead}>
          <h2>{icon}{title}</h2>
          {aside && <div className={styles.cardAside}>{aside}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

/** ⓘ 说明按钮：点一下展开一小段解释，再点或点别处收起。 */
export function InfoTip({ text, label = '查看说明' }: { text: ReactNode; label?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: Event) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [open]);
  return (
    <span className={styles.tipWrap} ref={ref}>
      <button type="button" className={styles.tipButton} aria-label={label} aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <Info size={15} strokeWidth={1.6} aria-hidden />
      </button>
      {open && <span className={styles.tipBubble} role="note">{text}</span>}
    </span>
  );
}

/** 分段开关（图片/视频、模拟/真实）。 */
export function Segmented<T extends string>({ value, options, onChange, ariaLabel }: {
  value: T;
  options: Array<{ value: T; label: ReactNode; sub?: string; disabled?: boolean; icon?: ReactNode }>;
  onChange: (value: T) => void;
  ariaLabel?: string;
}) {
  return (
    <div className={styles.segmented} role="group" aria-label={ariaLabel}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          disabled={option.disabled}
          aria-pressed={value === option.value}
          data-active={value === option.value}
          onClick={() => onChange(option.value)}
        >
          {option.icon}
          <span className={styles.segmentedText}>
            <span>{option.label}</span>
            {option.sub && <small>{option.sub}</small>}
          </span>
        </button>
      ))}
    </div>
  );
}

export function Pager({ page, pages, onChange }: { page: number; pages: number; onChange: (page: number) => void }) {
  if (pages <= 1) return null;
  return (
    <div className={styles.pager}>
      <button className="button secondary small" type="button" disabled={page <= 0} onClick={() => onChange(page - 1)}>上一页</button>
      <span className="muted small">{page + 1} / {pages}</span>
      <button className="button secondary small" type="button" disabled={page >= pages - 1} onClick={() => onChange(page + 1)}>下一页</button>
    </div>
  );
}
