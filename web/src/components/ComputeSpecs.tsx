import type { ComputeSpec } from '../lib/gpu-api';

export function ComputeSpecs({ specs }: { specs: ComputeSpec[] }) {
  return <div className="specList">{specs.map((spec, index) => {
    const key = spec.key;
    if (key && typeof key === 'object' && !Array.isArray(key)) {
      const value = key as Record<string, unknown>;
      const variant = String(value.variant || '');
      if (!['VARIANT_GPU', 'GPU'].includes(variant)) return null;
      const accelerator = String(value.accelerator || '未知 GPU');
      const shape = ['SHAPE_HIGHMEM', 'HIGHMEM'].includes(String(value.shape)) ? '高内存' : '标准内存';
      return <span className="pill" key={JSON.stringify(key)}>{accelerator} · {shape} · {spec.eligible === true ? '有申请资格' : '未确认申请资格'}</span>;
    }
    return <span className="pill" key={spec.id || spec.name || index}>{spec.name || spec.gpu || 'GPU'} · {spec.available === true ? '可用' : spec.available === false ? '不可用' : '状态未知'}{spec.reason ? ` · ${spec.reason}` : ''}</span>;
  })}<p className="muted">「有申请资格」不等于现在一定有空闲 GPU。检查不会开机、不扣 CU，也不会自动选用未确认的设备。</p></div>;
}
