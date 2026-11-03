package explorer

import (
	"context"
	"net/http/httptest"
	"runtime"
	"testing"

	model "github.com/cloudreve/Cloudreve/v3/models"
	"github.com/cloudreve/Cloudreve/v3/pkg/cache"
	"github.com/cloudreve/Cloudreve/v3/pkg/conf"
	"github.com/cloudreve/Cloudreve/v3/pkg/filesystem"
	"github.com/cloudreve/Cloudreve/v3/pkg/serializer"
	"github.com/gin-gonic/gin"
	"github.com/jinzhu/gorm"
	"github.com/stretchr/testify/assert"
)

func TestCheckMasterSavePath(t *testing.T) {
	a := assert.New(t)
	cache.Set("policy_7", model.Policy{Model: gorm.Model{ID: 7}, Type: "local", DirNameRule: "uploads/{uid}/{path}"}, 0)
	cache.Set("policy_8", model.Policy{Model: gorm.Model{ID: 8}, Type: "remote", DirNameRule: "uploads/{uid}/{path}"}, 0)
	cache.Set("policy_9", model.Policy{Model: gorm.Model{ID: 9}, Type: "local", DirNameRule: "{uid}/{path}"}, 0)
	cache.Set("policy_10", model.Policy{Model: gorm.Model{ID: 10}, Type: "local", DirNameRule: "uploads"}, 0)
	defer cache.Deletes([]string{"7", "8", "9", "10"}, "policy_")
	session := func(policy uint, savePath string) *serializer.UploadSession {
		return &serializer.UploadSession{Policy: model.Policy{Model: gorm.Model{ID: policy}}, SavePath: savePath}
	}

	// 主机自己生成的转存路径
	a.NoError(checkMasterSavePath(session(7, "uploads/1/download/1_abc_a.zip")))
	a.NoError(checkMasterSavePath(session(9, "1/download/1_abc_a.zip")))
	a.NoError(checkMasterSavePath(session(10, "uploads/1_abc_a.zip")))

	// 攻击用的路径：绝对路径、跳出策略目录、策略目录本身
	a.Error(checkMasterSavePath(session(7, "/root/.ssh/authorized_keys2")))
	a.Error(checkMasterSavePath(session(7, "uploads/../conf.ini")))
	a.Error(checkMasterSavePath(session(7, "uploads/1/../../cloudreve.db")))
	a.Error(checkMasterSavePath(session(7, "uploads")))
	a.Error(checkMasterSavePath(session(9, "../conf.ini")))
	// 目录规则没有固定前缀时，程序目录本身的文件（数据库、配置、二进制）也不能写
	a.Error(checkMasterSavePath(session(9, "cloudreve.db")))
	a.Error(checkMasterSavePath(session(9, "conf.ini")))
	a.Error(checkMasterSavePath(session(10, "uploads2/a.zip")))
	abs := "/etc/cron.d/x"
	if runtime.GOOS == "windows" {
		abs = `C:\Windows\x`
	}
	a.Error(checkMasterSavePath(session(9, abs)))

	// 非本地策略、不存在的策略
	a.Error(checkMasterSavePath(session(8, "uploads/1/a.zip")))
	a.Error(checkMasterSavePath(session(404, "uploads/1/a.zip")))
}

func TestSlaveCreateUploadSessionService_Create_Master(t *testing.T) {
	a := assert.New(t)
	mode := conf.SystemConfig.Mode
	conf.SystemConfig.Mode = "master"
	defer func() { conf.SystemConfig.Mode = mode }()
	cache.Set("policy_7", model.Policy{Model: gorm.Model{ID: 7}, Type: "local", DirNameRule: "uploads/{uid}/{path}"}, 0)
	defer cache.Deletes([]string{"7"}, "policy_")
	defer cache.Deletes([]string{"legit"}, filesystem.UploadSessionCachePrefix)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())

	service := SlaveCreateUploadSessionService{Session: serializer.UploadSession{
		Key: "forge", SavePath: "/root/.ssh/authorized_keys2", Policy: model.Policy{Model: gorm.Model{ID: 7}},
	}, TTL: 60}
	res := service.Create(context.Background(), c)
	a.Equal(serializer.CodeNoPermissionErr, res.Code)
	_, ok := cache.Get(filesystem.UploadSessionCachePrefix + "forge")
	a.False(ok)

	service.Session.SavePath = "uploads/1/a.zip"
	service.Session.Key = "legit"
	res = service.Create(context.Background(), c)
	a.Equal(0, res.Code)
}
