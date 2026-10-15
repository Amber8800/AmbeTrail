import type { GitContext } from './protocol';

export interface HistorySnapshot {
  id: string; endedAt: string; reason: string; internalBaseline: boolean; protected: boolean;
  title: string; context: GitContext;
}
export interface HistoryUsage { bytes: number; limit: number; snapshots: HistorySnapshot[] }
export const recordingFields = [
  { key: 'idleSeconds', label: '파일 변경 후 기록 대기', unit: '초', scale: 1, step: 5, fallback: 5,
    detail: '변경이 멈춘 뒤 기다리는 시간 · 5초 단위' },
  { key: 'maxIntervalSeconds', label: '연속 편집 중 최대 기록 대기', unit: '분', scale: 60, step: 1, fallback: 60,
    detail: '변경이 계속되어도 이 시간이 지나면 기록 · 1분 단위' },
  { key: 'fullHashSeconds', label: '전체 파일 내용 확인 주기', unit: '분', scale: 60, step: 5, fallback: 600,
    detail: '관리 대상 파일의 내용 해시 확인 · 5분 단위' },
] as const;
export type RecordingKey = typeof recordingFields[number]['key'];
export interface ManagementState {
  name: string; writable: boolean; paused: boolean; usage: HistoryUsage;
  recording: (typeof recordingFields[number] & { value: number; maximum: number })[];
}

export function recordingMaximum(field: typeof recordingFields[number]): number {
  return Math.floor(1_000_000 / field.scale / field.step) * field.step;
}

export function recordingValue(field: typeof recordingFields[number], raw: unknown, current: number): number {
  if ((typeof raw !== 'string' && typeof raw !== 'number') || String(raw).trim() === '') throw new Error(`${field.label}: 숫자를 입력하세요.`);
  const value = Number(raw);
  // Preserve legacy settings with finer intervals unless the user edits them.
  if (Number.isFinite(value) && value === current / field.scale) return current;
  if (!Number.isSafeInteger(value) || value < field.step || value > recordingMaximum(field) || value % field.step !== 0) {
    throw new Error(`${field.label}: ${field.step}${field.unit} 단위로 ${field.step}~${recordingMaximum(field)} 사이의 정수를 입력하세요.`);
  }
  return value * field.scale;
}
