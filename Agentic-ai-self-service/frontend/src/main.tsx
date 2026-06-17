import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './auth/configure';
import './index.css';
import App from './App.tsx';
import { ErrorBoundary } from './components/ErrorBoundary';

const needsAuth = !!import.meta.env.VITE_COGNITO_USER_POOL_ID;

async function render() {
  if (needsAuth) {
    const { Authenticator } = await import('@aws-amplify/ui-react');
    await import('@aws-amplify/ui-react/styles.css');

    createRoot(document.getElementById('root')!).render(
      <StrictMode>
        <ErrorBoundary>
          <Authenticator hideSignUp={true}>
            {() => <App />}
          </Authenticator>
        </ErrorBoundary>
      </StrictMode>,
    );
  } else {
    createRoot(document.getElementById('root')!).render(
      <StrictMode>
        <ErrorBoundary>
          <App />
        </ErrorBoundary>
      </StrictMode>,
    );
  }
}

render();
