# Keenetic - Keenetic Router Integration

![Keenetic Icon](static/Keenetic.png)

Integration with Keenetic routers for monitoring connected devices and network management.

## Description

The `Keenetic` module provides integration with Keenetic routers for the osysHome platform. It enables monitoring of connected devices, network statistics, and device management.

## Main Features

- ✅ **Device Monitoring**: Monitor devices connected to Keenetic router
- ✅ **Network Statistics**: View network usage statistics
- ✅ **Device Management**: Manage router devices
- ✅ **Multiple Routers**: Support for multiple Keenetic routers
- ✅ **Search Integration**: Search devices and routers
- ✅ **Widget Support**: Dashboard widget with router statistics

## Admin Panel

The module provides an admin interface for:
- Viewing routers
- Managing router configuration
- Viewing connected devices
- Configuring device settings

## Configuration

- **Router Settings**: Host, port, username, password
- **Update Interval**: Device update interval (default: 5 seconds)
- **Device Configuration**: Link devices to osysHome objects

## Usage

### Adding Router

1. Navigate to Keenetic module
2. Click "Add Router"
3. Enter router credentials
4. Save configuration
5. Devices discovered automatically

## Technical Details

- **API**: Keenetic HTTP API
- **Threading**: Parallel router processing
- **Device Discovery**: Automatic device discovery
- **Property Linking**: Link router devices to objects

## Version

Current version: **0.1**

## Category

Devices

## Actions

The module provides the following actions:
- `cycle` - Background device monitoring
- `search` - Search routers and devices
- `widget` - Dashboard widget

## Requirements

- Flask
- SQLAlchemy
- Requests
- osysHome core system

## Author

osysHome Team

## License

See the main osysHome project license

