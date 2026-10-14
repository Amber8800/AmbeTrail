import { ChildProcessWithoutNullStreams, spawn } from 'node:child_process';
import { EventEmitter } from 'node:events';

interface Pending { resolve(value: unknown): void; reject(error: Error): void; timer: NodeJS.Timeout }

/** One extension-owned Python process. Never replay mutation requests after disconnect. */
export class PythonBridge extends EventEmitter {
  private child: ChildProcessWithoutNullStreams;
  private sequence = 0;
  private pending = new Map<number, Pending>();
  private buffer = Buffer.alloc(0);
  private unavailable = false;
  private closing = false;
  private closed: Promise<void>;
  constructor(executable: string, entryPoint: string, cwd: string) {
    super();
    this.child = spawn(executable, ['-I', '-S', '-u', entryPoint], { cwd, shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    this.closed = new Promise(resolve => this.child.once('close', () => resolve()));
    this.child.stdout.on('data', (data: Buffer) => this.receive(data));
    // Python error output may include private paths. It is deliberately not logged.
    this.child.stderr.resume();
    this.child.stdin.on('error', () => this.fail(new Error('Python 연결이 끊겼습니다. 복구 기록을 검토한 뒤 창을 다시 로드하세요.')));
    this.child.on('error', () => this.fail(new Error('Python 실행 실패: 사용자 설정의 devSnapshot.pythonPath에 Python 3.12 이상 실행 파일을 지정하세요.')));
    this.child.on('close', () => { if (!this.closing) this.fail(new Error('Python 코어가 종료되었습니다. 작업을 자동 재시도하지 않습니다. 창을 다시 로드한 뒤 복구 상태를 확인하세요.')); });
  }
  async start(): Promise<void> {
    const hello = await this.call<{ protocol: number }>('hello', {}, 10_000);
    if (hello.protocol !== 1) throw new Error('지원하지 않는 Python 통신 버전입니다.');
  }
  call<T = unknown>(method: string, params: object = {}, timeoutMs = 180_000): Promise<T> {
    if (this.unavailable) return Promise.reject(new Error('Python 연결을 사용할 수 없습니다. 창을 다시 로드하고 복구 상태를 확인하세요.'));
    const id = ++this.sequence;
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => this.fail(new Error('Python 응답 시간이 초과되었습니다. 작업은 계속 실행 중일 수 있습니다. 중복 실행하지 말고 완료 또는 복구 상태를 확인하세요.')), timeoutMs);
      this.pending.set(id, { resolve: value => resolve(value as T), reject, timer });
      this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }
  notify(method: string, params: object): void {
    if (!this.unavailable && !this.closing) this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params }) + '\n');
  }
  private receive(chunk: Buffer): void {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    if (this.buffer.length > 32 * 1024 * 1024) { this.fail(new Error('Python 응답 크기 제한을 초과했습니다.')); return; }
    for (;;) {
      const end = this.buffer.indexOf(10); if (end < 0) break;
      const line = this.buffer.subarray(0, end); this.buffer = this.buffer.subarray(end + 1);
      let message: { jsonrpc?: string; id?: number; method?: string; params?: unknown; result?: unknown; error?: { message?: string } };
      try { message = JSON.parse(line.toString('utf8')); } catch { this.fail(new Error('Python 통신 형식이 잘못되었습니다.')); return; }
      if (message.jsonrpc !== '2.0') { this.fail(new Error('Python 통신 버전이 잘못되었습니다.')); return; }
      if (message.id !== undefined) {
        const pending = this.pending.get(message.id); if (!pending) continue;
        clearTimeout(pending.timer); this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message ?? 'Python 작업에 실패했습니다.'));
        else pending.resolve(message.result);
      } else if (message.method === 'changed') this.emit('changed', message.params);
    }
  }
  private fail(error: Error): void {
    if (this.unavailable) return;
    this.unavailable = true;
    for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(error); }
    this.pending.clear();
    if (!this.closing) this.emit('failure', error);
  }
  async stop(): Promise<void> {
    this.closing = true;
    if (!this.unavailable) await this.call('shutdown', {}, 5000).catch(() => undefined);
    this.child.stdin.end();
    await new Promise<void>(resolve => {
      const timer = setTimeout(resolve, 5000);
      void this.closed.then(() => { clearTimeout(timer); resolve(); });
    });
  }
}
