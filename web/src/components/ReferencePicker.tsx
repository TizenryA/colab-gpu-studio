import { Upload, Check } from 'lucide-react';
import type { GalleryAsset, GpuManifest } from '../lib/gpu-api';
import styles from '../gpu.module.css';

export function referenceAssets(assets: GalleryAsset[]): GalleryAsset[] {
  return assets.filter((asset) => !asset.mock && asset.metadata?.mock !== true && /^(image|video|audio)\//.test(asset.mime));
}

export function supportsReferences(manifest: GpuManifest | null): boolean {
  if (!manifest) return false;
  const declaration = manifest.assets as { required?: unknown[]; optional?: unknown[] } | undefined;
  if (declaration?.required?.length || declaration?.optional?.length) return true;
  const params = Array.isArray(manifest.parameters) ? manifest.parameters : Object.values(manifest.parameters || {});
  return params.some((p) => ['image', 'video', 'file', 'audio'].includes(String(p.type)) || p.widget === 'file');
}

export function ReferencePicker({ manifest, assets, selected, onChange, onUpload }: {
  manifest: GpuManifest | null; assets: GalleryAsset[]; selected: string[];
  onChange: (ids: string[]) => void; onUpload: (file: File) => Promise<void>;
}) {
  if (!manifest || !supportsReferences(manifest)) return null;
  const available = referenceAssets(assets);
  const toggle = (id: string) => onChange(selected.includes(id) ? selected.filter((item) => item !== id) : [...new Set([...selected, id])]);
  return (
    <div role="group" aria-label="参考素材" className={styles.refBox}>
      <div className={styles.refHead}>
        <span className="muted small">点选要用的图片或视频</span>
        <label className="button secondary small">
          <Upload size={14} strokeWidth={1.6} aria-hidden />上传
          <input hidden type="file" accept="image/*,video/*,audio/*" onChange={async (event) => {
            const file = event.target.files?.[0]; event.target.value = ''; if (file) await onUpload(file);
          }} />
        </label>
      </div>
      {available.length ? (
        <div className={styles.refGrid}>
          {available.map((asset) => {
            const on = selected.includes(asset.id);
            return (
              <label className={styles.refItem} data-on={on} key={asset.id} title={asset.filename || asset.id}>
                <input type="checkbox" hidden aria-label={`选用 ${asset.filename || asset.id}`} checked={on} onChange={() => toggle(asset.id)} />
                {asset.mime.startsWith('image/')
                  ? <img src={asset.thumbnail_url || `/api/assets/${asset.id}/thumbnail`} alt="" loading="lazy" />
                  : <span className={styles.refMedia}>{asset.mime.startsWith('video/') ? '视频' : '音频'}</span>}
                {on && <span className={styles.refCheck}><Check size={12} strokeWidth={2.4} aria-hidden /></span>}
              </label>
            );
          })}
        </div>
      ) : <p className="muted small">还没有素材，点右上角「上传」添加。</p>}
    </div>
  );
}
