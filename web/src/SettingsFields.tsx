interface NumberFieldProps {
  name: string;
  label: string;
  value: string;
  min: string;
  max: string;
  step?: string;
  suffix?: string;
  required?: boolean;
  onChange: (value: string) => void;
}

export function NumberField({
  name,
  label,
  value,
  min,
  max,
  step = "1",
  suffix,
  required = true,
  onChange,
}: NumberFieldProps) {
  return (
    <label>
      <span>{label}</span>
      <span className="settings-input-with-suffix">
        <input
          id={`settings-${name}`}
          name={`settings-${name}`}
          type="number"
          value={value}
          min={min}
          max={max}
          step={step}
          required={required}
          onChange={(event) => onChange(event.target.value)}
        />
        {suffix && <i>{suffix}</i>}
      </span>
    </label>
  );
}

interface SelectFieldProps {
  name: string;
  label: string;
  help?: string;
  value: string;
  options: Array<[string, string]>;
  onChange: (value: string) => void;
}

export function SelectField({
  name,
  label,
  help,
  value,
  options,
  onChange,
}: SelectFieldProps) {
  const knownValue = options.some(([option]) => option === value);
  return (
    <label>
      <span>{label}</span>
      <select
        id={`settings-${name}`}
        name={`settings-${name}`}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {!knownValue && <option value={value}>自定义（{value} Token）</option>}
        {options.map(([option, text]) => (
          <option key={option} value={option}>{text}</option>
        ))}
      </select>
      {help && <small>{help}</small>}
    </label>
  );
}
