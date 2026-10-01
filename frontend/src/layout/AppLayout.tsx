import { Layout, Menu, Space, Typography } from 'antd'
import {
  DashboardOutlined,
  DatabaseOutlined,
  SwapOutlined,
  HddOutlined,
  ClockCircleOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useTaskNotifications } from '../hooks/useTaskNotifications'
import NotificationBell from '../components/NotificationBell'
import GlobalSearch from '../components/GlobalSearch'

const { Sider, Header, Content } = Layout

const NAV_ITEMS = [
  { key: '/overview', icon: <DashboardOutlined />, label: '总览' },
  { key: '/snapshots', icon: <DatabaseOutlined />, label: '快照' },
  { key: '/diff', icon: <SwapOutlined />, label: '对比' },
  { key: '/disks', icon: <HddOutlined />, label: '磁盘' },
  { key: '/tasks', icon: <ClockCircleOutlined />, label: '任务' },
]

export default function AppLayout() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const { notifications, unreadCount, markAllRead } = useTaskNotifications()

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider width={200} theme="light" style={{ borderRight: '1px solid #f0f0f0' }}>
        <div style={{ padding: '16px 16px 8px' }}>
          <Typography.Title level={5} style={{ margin: 0 }}>
            冷备清单
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            cold-manifest
          </Typography.Text>
        </div>
        <Menu
          mode="inline"
          selectedKeys={[pathname]}
          items={NAV_ITEMS}
          onClick={({ key }) => navigate(key)}
        />
        <Menu
          mode="inline"
          selectable={false}
          style={{ position: 'absolute', bottom: 16, width: '100%', borderInlineEnd: 'none' }}
          items={[{ key: '/settings', icon: <SettingOutlined />, label: '设置' }]}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            background: '#fff',
            borderBottom: '1px solid #f0f0f0',
            padding: '0 24px',
            display: 'flex',
            alignItems: 'center',
          }}
        >
          <Space style={{ width: '100%', justifyContent: 'space-between' }}>
            <Typography.Text strong>冷备清单 · 元数据采集与比对</Typography.Text>
            <Space>
              <GlobalSearch />
              <NotificationBell
                notifications={notifications}
                unreadCount={unreadCount}
                onMarkAllRead={markAllRead}
              />
            </Space>
          </Space>
        </Header>
        <Content style={{ padding: 24, overflow: 'auto' }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
