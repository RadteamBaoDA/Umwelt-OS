/**
 * Settles a widget follow-up prompt for the prompt bridge: rejects when the session is busy or when
 * the send was not accepted by the server, resolves only on acceptance, so the sandbox frame gets
 * an honest `ok` flag instead of an optimistic one.
 *
 * @param busy - True while a response is pending or streaming.
 * @param send - Starts the send; resolves true only when the server accepted the message.
 * @param busyMessage - Localized rejection text for the busy case.
 * @param failedMessage - Localized rejection text for a failed/declined send.
 * @returns Promise resolving on acceptance; rejects with an Error otherwise.
 */
export async function settleWidgetPrompt(
  busy: boolean,
  send: () => Promise<boolean>,
  busyMessage: string,
  failedMessage: string,
): Promise<void> {
  if (busy) throw new Error(busyMessage);
  if (!(await send())) throw new Error(failedMessage);
}
