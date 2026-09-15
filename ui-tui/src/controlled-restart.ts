/** A ready receipt plus a clean protocol exit is required for respawn. */
export const RESTART_EXIT_CODE = 42;

export class ControlledBackendRestart {
  private ready: { requestId: string; session: string } | null = null;
  private restoring: string | null = null;

  acceptReady(event: { request_id: string; session: string; replayed?: boolean }): boolean {
    if (event.replayed || !/^[a-f0-9]{32}$/.test(event.request_id)
      || !event.session || /[\r\n\0/\\]/.test(event.session)) return false;
    this.ready = { requestId: event.request_id, session: event.session };
    return true;
  }

  cancel(): void { this.ready = null; }

  onExit(code: number | null): string | null {
    const ready = this.ready;
    this.ready = null;
    this.restoring = null;
    if (code !== RESTART_EXIT_CODE || !ready) return null;
    this.restoring = ready.session;
    return ready.session;
  }

  restored(session: string): boolean {
    if (this.restoring !== session) return false;
    this.restoring = null;
    return true;
  }
}
