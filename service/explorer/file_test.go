package explorer

import (
	"database/sql"
	"net/http/httptest"
	"testing"

	"github.com/DATA-DOG/go-sqlmock"
	model "github.com/cloudreve/Cloudreve/v3/models"
	"github.com/cloudreve/Cloudreve/v3/pkg/cache"
	"github.com/cloudreve/Cloudreve/v3/pkg/conf"
	"github.com/cloudreve/Cloudreve/v3/pkg/hashid"
	"github.com/cloudreve/Cloudreve/v3/pkg/serializer"
	"github.com/gin-gonic/gin"
	"github.com/jinzhu/gorm"
	"github.com/stretchr/testify/assert"
)

var mock sqlmock.Sqlmock

// TestMain 初始化数据库Mock
func TestMain(m *testing.M) {
	gin.SetMode(gin.TestMode)
	var db *sql.DB
	var err error
	db, mock, err = sqlmock.New()
	if err != nil {
		panic("An error was not expected when opening a stub database connection")
	}
	model.DB, _ = gorm.Open("mysql", db)
	defer db.Close()
	m.Run()
}

func TestSourceKeyService_Set(t *testing.T) {
	a := assert.New(t)
	conf.SystemConfig.HashIDSalt = ""
	cache.Set("setting_siteURL", "https://cloud.example.com", 0)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	fileID := hashid.HashID(2, hashid.FileID)

	// 用户组不使用中转外链
	{
		user := &model.User{Model: gorm.Model{ID: 1}}
		service := SourceKeyService{ID: fileID, Key: "98k-win"}
		res := service.Set(c, user)
		a.Equal(serializer.CodeGroupNotAllowed, res.Code)
		a.NoError(mock.ExpectationsWereMet())
	}

	user := &model.User{Model: gorm.Model{ID: 1}}
	user.Group.OptionsSerialized.RedirectedSource = true

	// 用户组的外链数量上限为 0
	{
		service := SourceKeyService{ID: fileID, Key: "98k-win"}
		res := service.Set(c, user)
		a.Equal(serializer.CodeBatchSourceSize, res.Code)
		a.NoError(mock.ExpectationsWereMet())
	}

	user.Group.OptionsSerialized.SourceBatchSize = 1

	// 文件 ID 无法解析
	{
		service := SourceKeyService{ID: "a/b", Key: "98k-win"}
		res := service.Set(c, user)
		a.Equal(serializer.CodeFileNotFound, res.Code)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 文件不属于当前用户
	{
		service := SourceKeyService{ID: fileID, Key: "98k-win"}
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(2, 1).WillReturnRows(sqlmock.NewRows([]string{"id"}))
		res := service.Set(c, user)
		a.Equal(serializer.CodeFileNotFound, res.Code)
		a.NoError(mock.ExpectationsWereMet())
	}

	expectSourceLink := func() {
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(2, 1).
			WillReturnRows(sqlmock.NewRows([]string{"id", "name", "user_id"}).AddRow(2, "deploy.sh", 1))
		mock.ExpectQuery("SELECT(.+)source_links(.+)").WithArgs(2).
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id"}).AddRow(1, 2))
	}

	// 成功，返回新的外链地址
	{
		service := SourceKeyService{ID: fileID, Key: "98k-win"}
		expectSourceLink()
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").WithArgs("98k-win", 1).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)source_links(.+)custom_key(.+)").WithArgs("98k-win", 1).
			WillReturnResult(sqlmock.NewResult(0, 1))
		mock.ExpectCommit()
		res := service.Set(c, user)
		a.Equal(0, res.Code)
		a.Equal("https://cloud.example.com/f/98k-win/deploy.sh", res.Data)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 已被另一个文件的外链使用
	{
		service := SourceKeyService{ID: fileID, Key: "98k-win"}
		expectSourceLink()
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").WithArgs("98k-win", 1).
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id"}).AddRow(7, 8))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(8).
			WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(8, "old.sh"))
		res := service.Set(c, user)
		a.Equal(serializer.CodeLinkKeyTaken, res.Code)
		a.Equal("old.sh", res.Msg)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 空值恢复为 HashID 链接
	{
		service := SourceKeyService{ID: fileID}
		expectSourceLink()
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)source_links(.+)custom_key(.+)").WithArgs("", 1).
			WillReturnResult(sqlmock.NewResult(0, 1))
		mock.ExpectCommit()
		res := service.Set(c, user)
		a.Equal(0, res.Code)
		a.Equal("https://cloud.example.com/f/"+hashid.HashID(1, hashid.SourceLinkID)+"/deploy.sh", res.Data)
		a.NoError(mock.ExpectationsWereMet())
	}
}
