/**
 * Reusable form field components for configuration modals.
 */

import { type ReactNode, type ChangeEvent } from 'react';

// ============================================================================
// Types
// ============================================================================

interface BaseFieldProps {
  label: string;
  id: string;
  error?: string;
  required?: boolean;
  helpText?: string;
}

// ============================================================================
// TextField Component
// ============================================================================

interface TextFieldProps extends BaseFieldProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: 'text' | 'password' | 'url';
  disabled?: boolean;
  maxLength?: number;
}

export function TextField({
  label,
  id,
  value,
  onChange,
  placeholder,
  type = 'text',
  error,
  required,
  helpText,
  disabled,
  maxLength,
}: TextFieldProps) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-gray-700">
        {label}
        {required && <span className="text-red-500 ml-1">*</span>}
      </label>
      <input
        id={id}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        maxLength={maxLength}
        className={`
          w-full px-3 py-2 text-sm border rounded-lg transition-colors
          ${error
            ? 'border-red-300 focus:ring-red-500 focus:border-red-500'
            : 'border-gray-300 focus:ring-blue-500 focus:border-blue-500'
          }
          ${disabled ? 'bg-gray-100 cursor-not-allowed' : 'bg-white'}
          focus:outline-none focus:ring-2
        `}
        data-testid={`field-${id}`}
      />
      {helpText && !error && (
        <p className="text-xs text-gray-500">{helpText}</p>
      )}
      {error && (
        <p className="text-xs text-red-600" data-testid={`error-${id}`}>{error}</p>
      )}
    </div>
  );
}

// ============================================================================
// TextArea Component
// ============================================================================

interface TextAreaProps extends BaseFieldProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  rows?: number;
  disabled?: boolean;
  footer?: ReactNode;
  maxLength?: number;
}

export function TextArea({
  label,
  id,
  value,
  onChange,
  placeholder,
  rows = 4,
  error,
  required,
  helpText,
  disabled,
  footer,
  maxLength,
}: TextAreaProps) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-gray-700">
        {label}
        {required && <span className="text-red-500 ml-1">*</span>}
      </label>
      <textarea
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        rows={rows}
        disabled={disabled}
        maxLength={maxLength}
        className={`
          w-full px-3 py-2 text-sm border rounded-lg transition-colors font-mono
          ${error
            ? 'border-red-300 focus:ring-red-500 focus:border-red-500'
            : 'border-gray-300 focus:ring-blue-500 focus:border-blue-500'
          }
          ${disabled ? 'bg-gray-100 cursor-not-allowed' : 'bg-white'}
          focus:outline-none focus:ring-2 resize-none
        `}
        data-testid={`field-${id}`}
      />
      {footer}
      {helpText && !error && (
        <p className="text-xs text-gray-500">{helpText}</p>
      )}
      {error && (
        <p className="text-xs text-red-600" data-testid={`error-${id}`}>{error}</p>
      )}
    </div>
  );
}

// ============================================================================
// SelectField Component
// ============================================================================

interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

interface SelectFieldProps extends BaseFieldProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  disabled?: boolean;
}

export function SelectField({
  label,
  id,
  value,
  onChange,
  options,
  placeholder,
  error,
  required,
  helpText,
  disabled,
}: SelectFieldProps) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-gray-700">
        {label}
        {required && <span className="text-red-500 ml-1">*</span>}
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className={`
          w-full px-3 py-2 text-sm border rounded-lg transition-colors
          ${error
            ? 'border-red-300 focus:ring-red-500 focus:border-red-500'
            : 'border-gray-300 focus:ring-blue-500 focus:border-blue-500'
          }
          ${disabled ? 'bg-gray-100 cursor-not-allowed' : 'bg-white'}
          focus:outline-none focus:ring-2
        `}
        data-testid={`field-${id}`}
      >
        {placeholder && (
          <option value="" disabled>
            {placeholder}
          </option>
        )}
        {options.map((option) => (
          <option key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
          </option>
        ))}
      </select>
      {helpText && !error && (
        <p className="text-xs text-gray-500">{helpText}</p>
      )}
      {error && (
        <p className="text-xs text-red-600" data-testid={`error-${id}`}>{error}</p>
      )}
    </div>
  );
}

// ============================================================================
// Toggle Component
// ============================================================================

interface ToggleProps {
  label: string;
  id: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  description?: string;
  disabled?: boolean;
}

export function Toggle({
  label,
  id,
  checked,
  onChange,
  description,
  disabled,
}: ToggleProps) {
  return (
    <div className="flex items-start gap-3">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => !disabled && onChange(!checked)}
        disabled={disabled}
        className={`
          relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out
          ${checked ? 'bg-blue-600' : 'bg-gray-200'}
          ${disabled ? 'opacity-50 cursor-not-allowed' : ''}
          focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2
        `}
        data-testid={`toggle-${id}`}
      >
        <span
          className={`
            pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out
            ${checked ? 'translate-x-5' : 'translate-x-0'}
          `}
        />
      </button>
      <div className="flex-1">
        <label htmlFor={id} className="text-sm font-medium text-gray-700">
          {label}
        </label>
        {description && (
          <p className="text-xs text-gray-500 mt-0.5">{description}</p>
        )}
      </div>
    </div>
  );
}

// ============================================================================
// NumberField Component
// ============================================================================

interface NumberFieldProps extends BaseFieldProps {
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
}

export function NumberField({
  label,
  id,
  value,
  onChange,
  min,
  max,
  step = 1,
  error,
  required,
  helpText,
  disabled,
}: NumberFieldProps) {
  const handleChange = (e: ChangeEvent<HTMLInputElement>) => {
    const newValue = parseFloat(e.target.value);
    if (!isNaN(newValue)) {
      onChange(newValue);
    }
  };

  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-gray-700">
        {label}
        {required && <span className="text-red-500 ml-1">*</span>}
      </label>
      <input
        id={id}
        type="number"
        value={value}
        onChange={handleChange}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        className={`
          w-full px-3 py-2 text-sm border rounded-lg transition-colors
          ${error
            ? 'border-red-300 focus:ring-red-500 focus:border-red-500'
            : 'border-gray-300 focus:ring-blue-500 focus:border-blue-500'
          }
          ${disabled ? 'bg-gray-100 cursor-not-allowed' : 'bg-white'}
          focus:outline-none focus:ring-2
        `}
        data-testid={`field-${id}`}
      />
      {helpText && !error && (
        <p className="text-xs text-gray-500">{helpText}</p>
      )}
      {error && (
        <p className="text-xs text-red-600" data-testid={`error-${id}`}>{error}</p>
      )}
    </div>
  );
}

// ============================================================================
// SliderField Component
// ============================================================================

interface SliderFieldProps extends BaseFieldProps {
  value: number;
  onChange: (value: number) => void;
  min: number;
  max: number;
  step?: number;
  disabled?: boolean;
  showValue?: boolean;
}

export function SliderField({
  label,
  id,
  value,
  onChange,
  min,
  max,
  step = 0.1,
  error,
  helpText,
  disabled,
  showValue = true,
}: SliderFieldProps) {
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <label htmlFor={id} className="block text-sm font-medium text-gray-700">
          {label}
        </label>
        {showValue && (
          <span className="text-sm text-gray-500">{value.toFixed(2)}</span>
        )}
      </div>
      <input
        id={id}
        type="range"
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        className={`
          w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer
          ${disabled ? 'opacity-50 cursor-not-allowed' : ''}
        `}
        data-testid={`field-${id}`}
      />
      <div className="flex justify-between text-xs text-gray-400">
        <span>{min}</span>
        <span>{max}</span>
      </div>
      {helpText && !error && (
        <p className="text-xs text-gray-500">{helpText}</p>
      )}
      {error && (
        <p className="text-xs text-red-600" data-testid={`error-${id}`}>{error}</p>
      )}
    </div>
  );
}

// ============================================================================
// FormSection Component
// ============================================================================

interface FormSectionProps {
  title?: string;
  description?: string;
  children: ReactNode;
}

export function FormSection({ title, description, children }: FormSectionProps) {
  return (
    <div className="space-y-4">
      {(title || description) && (
        <div>
          {title && <h3 className="text-sm font-semibold text-gray-800">{title}</h3>}
          {description && <p className="text-xs text-gray-500 mt-0.5">{description}</p>}
        </div>
      )}
      <div className="space-y-4">{children}</div>
    </div>
  );
}


// ============================================================================
// CheckboxField Component
// ============================================================================

interface CheckboxFieldProps extends BaseFieldProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}

export function CheckboxField({
  label,
  id,
  checked,
  onChange,
  error,
  helpText,
  disabled,
}: CheckboxFieldProps) {
  return (
    <div className="flex items-start space-x-3">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        disabled={disabled}
        className={`
          mt-1 h-4 w-4 rounded border-gray-300 text-blue-600
          focus:ring-blue-500
          ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
        `}
        data-testid={`field-${id}`}
      />
      <div className="flex-1">
        <label
          htmlFor={id}
          className={`block text-sm font-medium text-gray-700 ${disabled ? '' : 'cursor-pointer'}`}
        >
          {label}
        </label>
        {helpText && !error && (
          <p className="text-xs text-gray-500 mt-0.5">{helpText}</p>
        )}
        {error && (
          <p className="text-xs text-red-600 mt-0.5" data-testid={`error-${id}`}>{error}</p>
        )}
      </div>
    </div>
  );
}
