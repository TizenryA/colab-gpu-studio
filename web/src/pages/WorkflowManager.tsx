import { useMemo, useRef, useState } from 'react';
import { ChevronDown, Search } from 'lucide-react';
import type { GpuWorkflow } from '../lib/gpu-api';
import { Pager } from '../components/ui';
import styles from '../gpu.module.css';
import { workflowState } from '../lib/ui-helpers';

const GROUPS_PER_PAGE = 6;
const VERSIONS_SHOWN = 4;

const cmp = (a: GpuWorkflow, b: GpuWorkflow) =>
  String(b.workflow_version).localeCompare(String(a.workflow_version), undefined, { numeric: true });

export function WorkflowsPanel({
  workflows, onImport, onValidate, onPublish, onRollback,
}: {
  workflows: GpuWorkflow[];
  onImport: (bundle: string) => void;
  onValidate: (workflow: GpuWorkflow) => void;
  onPublish: (workflow: GpuWorkflow) => void;
  onRollback: (workflow: GpuWorkflow) => void;
}) {
  const [text, setText] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(0);
  const [openKeys, setOpenKeys] = useState<Set<string>>(new Set());
  const [showAll, setShowAll] = useState<Set<string>>(new Set());
  const fileRef = useRef<HTMLInputElement>(null);

  // 同一个 workflow_key（同一个模型流程）的所有版本收成一组，最新版本排最前
  const groups = useMemo(() => {
    const map = new Map<string, { key: string; modelId: string; items: GpuWorkflow[] }>();
    workflows.forEach((wf) => {
      const g = map.get(wf.workflow_key) || { key: wf.workflow_key, modelId: wf.model_id, items: [] };
      g.items.push(wf);
      map.set(wf.workflow_key, g);
    });
    const list = [...map.values()];
    list.forEach((g) => g.items.sort(cmp));
    return list.sort((a, b) => a.key.localeCompare(b.key));
  }, [workflows]);

  const q = query.trim().toLowerCase();
  const visible = groups.filter((g) => !q || `${g.key} ${g.modelId}`.toLowerCase().includes(q));
  const pages = Math.max(1, Math.ceil(visible.length / GROUPS_PER_PAGE));
  const safePage = Math.min(page, pages - 1);
  const slice = visible.slice(safePage * GROUPS_PER_PAGE, (safePage + 1) * GROUPS_PER_PAGE);

  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, key: string) => {
    const next = new Set(set);
    if (next.has(key)) next.delete(key); else next.add(key);
    setter(next);
  };

  return (
    <div className={styles.stack}>
      <details className="foldable">
        <summary>导入新工作流（高级）</summary>
        <div className="foldContent">
          <p className="muted small">粘贴或选择工作流 JSON（含 manifest、api_graph、object_info）。导入后只是草稿，需要校验、发布后才能使用。</p>
          <textarea className={styles.code} placeholder="在这里粘贴工作流 JSON" value={text} onChange={(e) => setText(e.target.value)} />
          <div className="actions">
            <button className="button secondary small" type="button" onClick={() => fileRef.current?.click()}>选择 JSON 文件</button>
            <input ref={fileRef} hidden type="file" accept="application/json,.json" onChange={async (e) => {
              const file = e.target.files?.[0];
              if (file) setText(await file.text());
              e.target.value = '';
            }} />
            <button className="button small" type="button" disabled={!text.trim()} onClick={() => onImport(text)}>导入为草稿</button>
          </div>
        </div>
      </details>

      <div className={styles.pageHead}>
        <p className="muted small">共 {groups.length} 个工作流、{workflows.length} 个版本。同一个工作流的版本收在一起，点开可查看全部。</p>
      </div>

      <label className={styles.searchBox}>
        <Search size={17} strokeWidth={1.6} aria-hidden />
        <input aria-label="搜索工作流" placeholder="搜索工作流名称" value={query} onChange={(e) => { setQuery(e.target.value); setPage(0); }} />
      </label>

      {slice.length ? (
        <div className={styles.list}>
          {slice.map((g) => {
            const current = g.items.find((i) => i.active);
            const isOpen = openKeys.has(g.key);
            const all = showAll.has(g.key);
            const shown = all ? g.items : g.items.slice(0, VERSIONS_SHOWN);
            return (
              <article className={styles.wfGroup} key={g.key} data-open={isOpen}>
                <button type="button" className={styles.wfGroupHead} aria-expanded={isOpen} onClick={() => toggle(openKeys, setOpenKeys, g.key)}>
                  <span>
                    <strong>{g.key}</strong>
                    <small className="muted">{g.modelId} · {g.items.length} 个版本</small>
                  </span>
                  {current ? <span className="pill solid">当前 v{current.workflow_version}</span> : <span className="pill">未启用</span>}
                  <ChevronDown size={16} strokeWidth={1.6} aria-hidden className={styles.chev} />
                </button>
                {isOpen && (
                  <div className={styles.versionList}>
                    {shown.map((wf) => {
                      const published = wf.state === 'published';
                      return (
                        <div className={styles.versionItem} key={wf.id}>
                          <span className={styles.versionName}>
                            <strong>v{wf.workflow_version}</strong>
                            <span className="pill" data-state={wf.active ? 'active' : wf.state}>{workflowState(wf.state, wf.active)}</span>
                          </span>
                          <span className={styles.versionActions}>
                            {!published && <button className="button secondary small" type="button" onClick={() => onValidate(wf)}>校验</button>}
                            {!published && <button className="button small" type="button" onClick={() => onPublish(wf)}>发布</button>}
                            {published && !wf.active && <button className="button secondary small" type="button" onClick={() => onRollback(wf)}>设为当前</button>}
                          </span>
                        </div>
                      );
                    })}
                    {g.items.length > VERSIONS_SHOWN && (
                      <button type="button" className="button ghost small" onClick={() => toggle(showAll, setShowAll, g.key)}>
                        {all ? '收起旧版本' : `显示全部 ${g.items.length} 个版本`}
                      </button>
                    )}
                  </div>
                )}
              </article>
            );
          })}
        </div>
      ) : (
        <div className="empty"><strong>{workflows.length ? '没有匹配的工作流' : '还没有工作流'}</strong></div>
      )}

      <Pager page={safePage} pages={pages} onChange={setPage} />
    </div>
  );
}
