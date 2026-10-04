/**
 * The tool-test sandbox has no route to the internet (ARCC: isolation for
 * customer-provided code), so a generated tool that calls an HTTP API fails its
 * test and works once deployed. That is an acceptable trade-off only if the panel
 * says so instead of presenting it as a code failure.
 *
 * The expensive half is the auto-fix loop. Left alone it fires on any failure,
 * which means it asks the model — twice, MAX_AUTO_FIX_RETRIES — to "repair" code
 * that is already correct. The only repair available is to delete the network call
 * the user asked for, so the loop's success condition is a silently worse tool
 * that passes. Two Bedrock calls to make the product worse.
 *
 * So: when `note` is present, no auto-fix, no button, and the explanation renders.
 * And when it is absent, auto-fix must still work — a suppression tested only by
 * what it suppresses is compatible with suppressing everything.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ToolGeneratorPanel } from './ToolGeneratorPanel';
import * as api from '../../services/api';

const TOOL = {
  toolName: 'get_weather',
  displayName: 'Get Weather',
  description: 'Fetches weather for a city',
  // Correct code. It calls out, which is the whole point: nothing here is broken.
  lambdaCode: 'def lambda_handler(event, context):\n    return {"ok": True}\n',
  inputSchema: { type: 'object', properties: { city: { type: 'string' } } },
};

const TEST_CASES = [
  { name: 'london', input: { city: 'London' }, expectedOutputKeys: ['temp'], description: 'A city' },
];

const NETWORK_FAILURE = [
  {
    testCaseName: 'london',
    passed: false,
    error: '<urlopen error [Errno -3] Temporary failure in name resolution>',
    durationMs: 812,
  },
];

const LOGIC_FAILURE = [
  { testCaseName: 'london', passed: false, error: "KeyError: 'temp'", durationMs: 44 },
];

const SANDBOX_NOTE =
  'One or more test cases failed on a network call. The tool-test sandbox runs with ' +
  'no internet access by design, so outbound HTTP calls cannot succeed here even ' +
  'when the tool is correct.';

function generated() {
  return {
    success: true,
    message: 'Generated tool: Get Weather',
    responseType: 'generation' as const,
    tool: TOOL,
    testCases: TEST_CASES,
  };
}

/** Drive the panel the way a user does: type a description, press Enter. */
async function describeATool(
  onAddToolToCanvas = vi.fn(),
  submitWith: 'enter' | 'button' = 'enter',
) {
  render(
    <ToolGeneratorPanel
      isVisible
      onClose={() => {}}
      onAddToolToCanvas={onAddToolToCanvas}
    />,
  );
  const input = await screen.findByPlaceholderText(/describe/i);
  fireEvent.change(input, { target: { value: 'a tool that fetches the weather' } });
  if (submitWith === 'button') {
    const send = screen.getByRole('button', { name: 'Send tool generation request' });
    send.focus();
    fireEvent.click(send);
  } else {
    fireEvent.keyDown(input, { key: 'Enter' });
  }
  return input;
}

describe('ToolGeneratorPanel — an isolated sandbox is not a broken tool', () => {
  let generateSpy: ReturnType<typeof vi.spyOn>;
  let testSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    generateSpy = vi.spyOn(api, 'generateToolApi').mockResolvedValue(generated());
    testSpy = vi.spyOn(api, 'testToolApi');
  });

  it('does not auto-fix when the sandbox is what failed the test', async () => {
    testSpy.mockResolvedValue({
      success: true,
      allPassed: false,
      results: NETWORK_FAILURE,
      sandboxIsolated: true,
      note: SANDBOX_NOTE,
    });

    await describeATool();
    await waitFor(() => expect(testSpy).toHaveBeenCalledTimes(1));

    // The one call is the user's own generation. A second would be the auto-fix
    // asking the model to delete the network call.
    await waitFor(() => expect(generateSpy).toHaveBeenCalledTimes(1));
    expect(testSpy).toHaveBeenCalledTimes(1);
  });

  it('explains why, rather than leaving a timeout from correct code unexplained', async () => {
    testSpy.mockResolvedValue({
      success: true,
      allPassed: false,
      results: NETWORK_FAILURE,
      sandboxIsolated: true,
      note: SANDBOX_NOTE,
    });

    await describeATool();

    expect(await screen.findByText(/Network calls cannot be tested here/i)).toBeTruthy();
    expect(await screen.findByText(/no internet access by design/i)).toBeTruthy();
    expect(
      await screen.findByText(NETWORK_FAILURE[0].error),
    ).toHaveClass('text-red-700');
  });

  it('hides the auto-fix button too, not just the automatic loop', async () => {
    testSpy.mockResolvedValue({
      success: true,
      allPassed: false,
      results: NETWORK_FAILURE,
      sandboxIsolated: true,
      note: SANDBOX_NOTE,
    });

    await describeATool();
    await screen.findByText(/Network calls cannot be tested here/i);

    // Suppressing only the loop would leave a button that does the same damage on
    // one click, offered to a user who has just been told the tool is fine.
    expect(screen.queryByRole('button', { name: /auto-fix/i })).toBeNull();
  });

  it('lets the user add a network tool for deployment-time validation', async () => {
    testSpy.mockResolvedValue({
      success: true,
      allPassed: false,
      results: NETWORK_FAILURE,
      sandboxIsolated: true,
      note: SANDBOX_NOTE,
    });
    const onAddToolToCanvas = vi.fn();

    await describeATool(onAddToolToCanvas);

    const addForDeployment = await screen.findByRole('button', {
      name: /add to canvas.*test after deployment/i,
    });
    fireEvent.click(addForDeployment);

    expect(onAddToolToCanvas).toHaveBeenCalledTimes(1);
    expect(onAddToolToCanvas).toHaveBeenCalledWith(TOOL);
  });

  it('makes the generated Lambda code a named keyboard-scrollable region', async () => {
    testSpy.mockResolvedValue({
      success: true,
      allPassed: true,
      results: [{ testCaseName: 'london', passed: true, durationMs: 40 }],
      sandboxIsolated: true,
    });

    await describeATool();

    fireEvent.click(await screen.findByRole('button', { name: /view lambda code/i }));
    expect(
      screen.getByRole('region', { name: 'Generated Lambda code for Get Weather' }),
    ).toHaveAttribute('tabindex', '0');
  });

  it('makes failed-test output a named keyboard-scrollable region', async () => {
    testSpy.mockResolvedValue({
      success: true,
      allPassed: false,
      results: [
        {
          ...NETWORK_FAILURE[0],
          actualOutput: { error: 'dns lookup failed', requestId: 'req-123' },
        },
      ],
      sandboxIsolated: true,
      note: SANDBOX_NOTE,
    });

    await describeATool();

    fireEvent.click(await screen.findByText('Show output'));
    expect(
      screen.getByRole('region', { name: 'Actual output for london' }),
    ).toHaveAttribute('tabindex', '0');
  });

  it('returns focus to the description after generation and testing finish', async () => {
    testSpy.mockResolvedValue({
      success: true,
      allPassed: true,
      results: [{ testCaseName: 'london', passed: true, durationMs: 40 }],
      sandboxIsolated: true,
    });

    const input = await describeATool(vi.fn(), 'button');
    await screen.findByText('Tests Passed');

    await waitFor(() => expect(document.activeElement).toBe(input));
  });

  it('does not steal focus when the user moves to another control while waiting', async () => {
    let finishGeneration!: (value: ReturnType<typeof generated>) => void;
    generateSpy.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishGeneration = resolve;
        }),
    );
    testSpy.mockResolvedValue({
      success: true,
      allPassed: true,
      results: [{ testCaseName: 'london', passed: true, durationMs: 40 }],
      sandboxIsolated: true,
    });

    await describeATool(vi.fn(), 'button');
    const close = screen.getByRole('button', { name: 'Close the AI tool generator' });
    close.focus();
    finishGeneration(generated());
    await screen.findByText('Tests Passed');

    expect(document.activeElement).toBe(close);
  });

  it('still auto-fixes a genuine logic failure', async () => {
    // The happy path of the suppression. Without this, "never auto-fix" passes
    // every test above, and the feature is quietly gone.
    testSpy
      .mockResolvedValueOnce({
        success: true,
        allPassed: false,
        results: LOGIC_FAILURE,
        sandboxIsolated: true,
      })
      .mockResolvedValue({
        success: true,
        allPassed: true,
        results: [{ testCaseName: 'london', passed: true, durationMs: 40 }],
        sandboxIsolated: true,
      });

    await describeATool();

    // Twice: the user's generation, then the auto-fix request.
    await waitFor(() => expect(generateSpy).toHaveBeenCalledTimes(2));
    const fixPrompt = generateSpy.mock.calls[1][0] as { prompt: string };
    expect(fixPrompt.prompt).toContain("KeyError: 'temp'");
  });

  it('shows no explanation when there is nothing to explain', async () => {
    testSpy.mockResolvedValue({
      success: true,
      allPassed: false,
      results: LOGIC_FAILURE,
      sandboxIsolated: true,
    });

    await describeATool();
    await waitFor(() => expect(testSpy).toHaveBeenCalled());

    expect(screen.queryByText(/Network calls cannot be tested here/i)).toBeNull();
  });
});
