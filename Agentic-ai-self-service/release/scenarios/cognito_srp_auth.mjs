// Authenticate a synthetic Cognito user exactly as the shipped frontend does: the
// frontend's own pinned aws-amplify (resolved through frontend/package.json), the
// USER_SRP_AUTH flow, an in-memory token store, and a sign-out in every exit path.
// Reads one JSON object on stdin and writes one on stdout; the access token never
// touches a file or an argv. `{"probe": true}` reports the pinned versions only.
import { createRequire } from "node:module";

class MemoryStorage {
  constructor() {
    this.values = new Map();
  }

  async setItem(key, value) {
    this.values.set(key, value);
  }

  async getItem(key) {
    return this.values.get(key) ?? null;
  }

  async removeItem(key) {
    this.values.delete(key);
  }

  async clear() {
    this.values.clear();
  }
}

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Input must be a JSON object");
  }
  return value;
}

function requireText(input, name) {
  const value = input[name];
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${name} must be a non-empty string`);
  }
  return value;
}

async function main() {
  const input = await readInput();
  const packageJson = requireText(input, "frontendPackageJson");
  const requireFromFrontend = createRequire(packageJson);
  const { Amplify } = requireFromFrontend("aws-amplify");
  const { fetchAuthSession, signIn, signOut } = requireFromFrontend("aws-amplify/auth");
  const { cognitoUserPoolsTokenProvider } = requireFromFrontend("aws-amplify/auth/cognito");
  const amplifyVersion = requireFromFrontend("aws-amplify/package.json").version;
  const authVersion = requireFromFrontend("@aws-amplify/auth/package.json").version;

  const storage = new MemoryStorage();
  cognitoUserPoolsTokenProvider.setKeyValueStorage(storage);

  if (input.probe === true) {
    process.stdout.write(
      JSON.stringify({
        amplifyVersion,
        authVersion,
        authFlowType: "USER_SRP_AUTH",
        tokenStore: "memory",
      }),
    );
    return;
  }

  const userPoolId = requireText(input, "userPoolId");
  const userPoolClientId = requireText(input, "userPoolClientId");
  const username = requireText(input, "username");
  const password = requireText(input, "password");

  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId,
        userPoolClientId,
        loginWith: { email: true },
      },
    },
  });

  try {
    const result = await signIn({
      username,
      password,
      options: { authFlowType: "USER_SRP_AUTH" },
    });
    if (!result.isSignedIn) {
      throw new Error(`SRP did not complete: ${result.nextStep?.signInStep ?? "unknown"}`);
    }
    const session = await fetchAuthSession({ forceRefresh: true });
    const accessToken = session.tokens?.accessToken;
    if (!accessToken || accessToken.payload?.token_use !== "access") {
      throw new Error("Cognito did not return an access token");
    }
    process.stdout.write(
      JSON.stringify({
        accessToken: accessToken.toString(),
        tokenUse: accessToken.payload.token_use,
        groups: accessToken.payload["cognito:groups"] ?? [],
        amplifyVersion,
        authVersion,
      }),
    );
  } finally {
    try {
      await signOut();
    } finally {
      await storage.clear();
    }
  }
}

main().catch((error) => {
  const name = error instanceof Error ? error.name : "Error";
  const message = error instanceof Error ? error.message : "SRP authentication failed";
  process.stderr.write(`${name}: ${message}\n`);
  process.exitCode = 1;
});
