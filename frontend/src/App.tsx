import { Navigate, Route, Routes } from 'react-router-dom'
import AppLayout from './layout/AppLayout'
import OverviewPage from './pages/OverviewPage'
import SnapshotsPage from './pages/SnapshotsPage'
import DiffPage from './pages/DiffPage'
import DisksPage from './pages/DisksPage'
import TasksPage from './pages/TasksPage'
import SettingsPage from './pages/SettingsPage'

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route path="/" element={<Navigate to="/overview" replace />} />
        <Route path="/overview" element={<OverviewPage />} />
        <Route path="/snapshots" element={<SnapshotsPage />} />
        <Route path="/diff" element={<DiffPage />} />
        <Route path="/disks" element={<DisksPage />} />
        <Route path="/tasks" element={<TasksPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
    </Routes>
  )
}
