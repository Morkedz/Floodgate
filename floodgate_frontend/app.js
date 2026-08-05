// 1. Initialize Supabase Client
const SUPABASE_URL = 'https://xthgzjgwabyhlsubuokl.supabase.co/rest/v1/';
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inh0aGd6amd3YWJ5aGxzdWJ1b2tsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU0NDY3MDQsImV4cCI6MjEwMTAyMjcwNH0.Jfu3zK7IY0HC5Kkx6ofJN8RzAijdzSpsvjyYXgNyMpE';
const supabaseClient = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// Helper for live chart creation
function createLineChart(ctx, label, color) {
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels: [], // Timestamps
      datasets: [{
        label: label,
        data: [],
        borderColor: color,
        borderWidth: 2,
        tension: 0.3,
        pointRadius: 0
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { display: false },
        y: { grid: { color: '#2d323e' }, ticks: { color: '#8a8d93' } }
      },
      plugins: { legend: { display: false } }
    }
  });
}

// 2. Initialize Charts
const pressureChart = createLineChart(document.getElementById('pressureChart'), 'Pressure', '#a0a0a0');
const tempChart     = createLineChart(document.getElementById('tempChart'), 'Temp', '#ff4d4d');
const waterChart    = createLineChart(document.getElementById('waterChart'), 'Water Depth', '#0088ff');

// Helper to push new data onto charts
function addDataToChart(chart, timeLabel, value) {
  chart.data.labels.push(timeLabel);
  chart.data.datasets[0].data.push(value);
  
  // Keep only the last 20 data points on screen
  if (chart.data.labels.length > 20) {
    chart.data.labels.shift();
    chart.data.datasets[0].data.shift();
  }
  chart.update();
}

// 3. Listen for Real-Time Sensor Updates from Supabase
supabaseClient
  .channel('ui-telemetry-stream')
  .on(
    'postgres_changes',
    { event: 'INSERT', schema: 'public', table: 'sensor_readings' },
    (payload) => {
      console.log('NEW ROW DETECTED IN REALTIME:', payload.new);

      const data = payload.new;
      const time = new Date(data.created_at).toLocaleTimeString();

      // Update Charts
      if (data.atm_pressure_hpa) addDataToChart(pressureChart, time, data.atm_pressure_hpa);
      if (data.ambient_temp_c)   addDataToChart(tempChart, time, data.ambient_temp_c);
      if (data.water_depth)      addDataToChart(waterChart, time, data.water_depth);

      document.getElementById('last-updated').innerText = `Last Updated: ${time}`;
    }
  )
  .subscribe();

// 4. Listen for Analytics Updates (Flood & Clog Likelihood)
supabaseClient
  .channel('ui-analytics-stream')
  .on(
    'postgres_changes',
    { event: 'INSERT', schema: 'public', table: 'drain_analytics' },
    (payload) => {
      const data = payload.new;
      if (data.flood_likelihood !== undefined) {
        document.getElementById('flood-likelihood').innerText = `${data.flood_likelihood}%`;
      }
      if (data.clog_likelihood !== undefined) {
        document.getElementById('clog-likelihood').innerText = `${data.clog_likelihood}%`;
      }
    }
  )
  .subscribe();