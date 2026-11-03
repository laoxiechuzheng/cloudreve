package model

import (
	"context"
	"github.com/cloudreve/Cloudreve/v3/models/scripts/invoker"
	"github.com/cloudreve/Cloudreve/v3/pkg/cache"
	"github.com/cloudreve/Cloudreve/v3/pkg/conf"
	"github.com/cloudreve/Cloudreve/v3/pkg/util"
	"github.com/fatih/color"
	"github.com/hashicorp/go-version"
	"github.com/jinzhu/gorm"
	"sort"
	"strings"
)

// 是否需要迁移
func needMigration() bool {
	var setting Setting
	return DB.Where("name = ?", "db_version_"+conf.RequiredDBVersion).First(&setting).Error != nil
}

// 执行数据迁移
func migration() {
	// 确认是否需要执行迁移
	if !needMigration() {
		util.Log().Info("Database version fulfilled, skip schema migration.")
		return

	}

	util.Log().Info("Start initializing database schema...")

	// 清除所有缓存
	if instance, ok := cache.Store.(*cache.RedisStore); ok {
		instance.DeleteAll()
	}

	// 自动迁移模式
	if conf.DatabaseConfig.Type == "mysql" {
		DB = DB.Set("gorm:table_options", "ENGINE=InnoDB")
	}

	DB.AutoMigrate(&User{}, &Setting{}, &Group{}, &Policy{}, &Folder{}, &File{}, &Share{},
		&Task{}, &Download{}, &Tag{}, &Webdav{}, &Node{}, &SourceLink{})

	// 创建初始存储策略
	addDefaultPolicy()

	// 创建初始用户组
	addDefaultGroups()

	// 创建初始管理员账户
	addDefaultUser()

	// 创建初始节点
	addDefaultNode()

	// 向设置数据表添加初始设置
	addDefaultSettings()

	// 执行数据库升级脚本
	execUpgradeScripts()

	util.Log().Info("Finish initializing database schema.")

}

// ensureCustomLinkKeys 为已有数据库补上 shares.custom_key、source_links.custom_key 列和索引。
// 不改数据库版本号；换回上游二进制时这两列被忽略，分享和外链回到 HashID 链接。
func ensureCustomLinkKeys() {
	for _, value := range []interface{}{&Share{}, &SourceLink{}} {
		scope := DB.NewScope(value)
		if scope.Dialect().HasColumn(scope.TableName(), "custom_key") {
			continue
		}
		if err := DB.AutoMigrate(value).Error; err != nil {
			util.Log().Panic("Failed to add custom link key column to %s: %s", scope.TableName(), err)
		}
		util.Log().Info("Added custom link key column to %s.", scope.TableName())
	}
}

// ensureNodeKeys 给通信密钥为空或过短的节点换上随机密钥（每次启动检查）。
// 上游创建的主节点两个密钥都是空的，而从机回调接口用 MasterKey 验签，空密钥等于不验签。
// 主节点的两个密钥没有其他用途；从机节点的 MasterKey 由主机在心跳里下发给从机，可以直接换。
// 从机节点的 SlaveKey 必须与从机配置里的 Secret 一致，这里不动。
func ensureNodeKeys() {
	var nodes []Node
	if err := DB.Find(&nodes).Error; err != nil {
		util.Log().Panic("Failed to read nodes: %s", err)
	}
	for _, node := range nodes {
		update := map[string]interface{}{}
		if len(node.MasterKey) < NodeKeyMinLength {
			update["master_key"] = NewNodeKey()
		}
		if node.Type == MasterNodeType && len(node.SlaveKey) < NodeKeyMinLength {
			update["slave_key"] = NewNodeKey()
		}
		if len(update) == 0 {
			continue
		}
		if err := DB.Model(&Node{}).Where("id = ?", node.ID).UpdateColumns(update).Error; err != nil {
			util.Log().Panic("Failed to set communication keys for node %d: %s", node.ID, err)
		}
		util.Log().Warning("Node %d had an empty or short communication key; a random one was generated.", node.ID)
	}
}

func addDefaultPolicy() {
	_, err := GetPolicyByID(uint(1))
	// 未找到初始存储策略时，则创建
	if gorm.IsRecordNotFoundError(err) {
		defaultPolicy := Policy{
			Name:               "Default storage policy",
			Type:               "local",
			MaxSize:            0,
			AutoRename:         true,
			DirNameRule:        "uploads/{uid}/{path}",
			FileNameRule:       "{uid}_{randomkey8}_{originname}",
			IsOriginLinkEnable: false,
			OptionsSerialized: PolicyOption{
				ChunkSize: 25 << 20, // 25MB
			},
		}
		if err := DB.Create(&defaultPolicy).Error; err != nil {
			util.Log().Panic("Failed to create default storage policy: %s", err)
		}
	}
}

func addDefaultSettings() {
	for _, value := range defaultSettings {
		DB.Where(Setting{Name: value.Name}).Create(&value)
	}
}

func addDefaultGroups() {
	_, err := GetGroupByID(1)
	// 未找到初始管理组时，则创建
	if gorm.IsRecordNotFoundError(err) {
		defaultAdminGroup := Group{
			Name:          "Admin",
			PolicyList:    []uint{1},
			MaxStorage:    1 * 1024 * 1024 * 1024,
			ShareEnabled:  true,
			WebDAVEnabled: true,
			OptionsSerialized: GroupOption{
				ArchiveDownload:  true,
				ArchiveTask:      true,
				ShareDownload:    true,
				Aria2:            true,
				SourceBatchSize:  1000,
				Aria2BatchSize:   50,
				RedirectedSource: true,
				AdvanceDelete:    true,
			},
		}
		if err := DB.Create(&defaultAdminGroup).Error; err != nil {
			util.Log().Panic("Failed to create admin user group: %s", err)
		}
	}

	err = nil
	_, err = GetGroupByID(2)
	// 未找到初始注册会员时，则创建
	if gorm.IsRecordNotFoundError(err) {
		defaultAdminGroup := Group{
			Name:          "User",
			PolicyList:    []uint{1},
			MaxStorage:    1 * 1024 * 1024 * 1024,
			ShareEnabled:  true,
			WebDAVEnabled: true,
			OptionsSerialized: GroupOption{
				ShareDownload:    true,
				SourceBatchSize:  10,
				Aria2BatchSize:   1,
				RedirectedSource: true,
			},
		}
		if err := DB.Create(&defaultAdminGroup).Error; err != nil {
			util.Log().Panic("Failed to create initial user group: %s", err)
		}
	}

	err = nil
	_, err = GetGroupByID(3)
	// 未找到初始游客用户组时，则创建
	if gorm.IsRecordNotFoundError(err) {
		defaultAdminGroup := Group{
			Name:       "Anonymous",
			PolicyList: []uint{},
			Policies:   "[]",
			OptionsSerialized: GroupOption{
				ShareDownload: true,
			},
		}
		if err := DB.Create(&defaultAdminGroup).Error; err != nil {
			util.Log().Panic("Failed to create anonymous user group: %s", err)
		}
	}
}

func addDefaultUser() {
	_, err := GetUserByID(1)
	password := util.RandStringRunes(8)

	// 未找到初始用户时，则创建
	if gorm.IsRecordNotFoundError(err) {
		defaultUser := NewUser()
		defaultUser.Email = "admin@cloudreve.org"
		defaultUser.Nick = "admin"
		defaultUser.Status = Active
		defaultUser.GroupID = 1
		err := defaultUser.SetPassword(password)
		if err != nil {
			util.Log().Panic("Failed to create password: %s", err)
		}
		if err := DB.Create(&defaultUser).Error; err != nil {
			util.Log().Panic("Failed to create initial root user: %s", err)
		}

		c := color.New(color.FgWhite).Add(color.BgBlack).Add(color.Bold)
		util.Log().Info("Admin user name: " + c.Sprint("admin@cloudreve.org"))
		util.Log().Info("Admin password: " + c.Sprint(password))
	}
}

func addDefaultNode() {
	_, err := GetNodeByID(1)

	if gorm.IsRecordNotFoundError(err) {
		defaultAdminGroup := Node{
			Name:      "Master (Local machine)",
			Status:    NodeActive,
			Type:      MasterNodeType,
			MasterKey: NewNodeKey(),
			SlaveKey:  NewNodeKey(),
			Aria2OptionsSerialized: Aria2Option{
				Interval: 10,
				Timeout:  10,
			},
		}
		if err := DB.Create(&defaultAdminGroup).Error; err != nil {
			util.Log().Panic("Failed to create initial node: %s", err)
		}
	}
}

func execUpgradeScripts() {
	s := invoker.ListPrefix("UpgradeTo")
	versions := make([]*version.Version, len(s))
	for i, raw := range s {
		v, _ := version.NewVersion(strings.TrimPrefix(raw, "UpgradeTo"))
		versions[i] = v
	}
	sort.Sort(version.Collection(versions))

	for i := 0; i < len(versions); i++ {
		invoker.RunDBScript("UpgradeTo"+versions[i].String(), context.Background())
	}
}
