import { Component, type ReactNode } from 'react';

// Catches any uncaught render error in the React tree so a single component
// crash doesn't blank-screen the entire app. See colleague audit issue #5.

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('ErrorBoundary caught:', error, info);
  }

  handleReset = () => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError) {
      return (
        <div
          role="alert"
          style={{
            padding: '2rem',
            margin: '2rem auto',
            maxWidth: '640px',
            border: '1px solid #f44336',
            borderRadius: '8px',
            background: '#fff5f5',
            color: '#a00',
            fontFamily: 'system-ui, -apple-system, sans-serif',
          }}
        >
          <h2 style={{ marginTop: 0 }}>Something went wrong</h2>
          <p>The interface hit an unexpected error. Your last save is still in DynamoDB.</p>
          {this.state.error && (
            <pre
              style={{
                whiteSpace: 'pre-wrap',
                background: '#fff',
                padding: '0.75rem',
                borderRadius: '4px',
                fontSize: '0.85em',
                overflow: 'auto',
                maxHeight: '200px',
              }}
            >
              {this.state.error.message}
            </pre>
          )}
          <button
            type="button"
            onClick={this.handleReset}
            style={{
              padding: '0.5rem 1rem',
              borderRadius: '4px',
              border: 'none',
              background: '#1976d2',
              color: '#fff',
              cursor: 'pointer',
              marginRight: '0.5rem',
            }}
          >
            Try again
          </button>
          <button
            type="button"
            onClick={() => window.location.reload()}
            style={{
              padding: '0.5rem 1rem',
              borderRadius: '4px',
              border: '1px solid #ccc',
              background: '#fff',
              cursor: 'pointer',
            }}
          >
            Reload page
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
