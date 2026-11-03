package middleware

import (
	"errors"
	model "github.com/cloudreve/Cloudreve/v3/models"
	"github.com/cloudreve/Cloudreve/v3/pkg/aria2/common"
	"github.com/cloudreve/Cloudreve/v3/pkg/auth"
	"github.com/cloudreve/Cloudreve/v3/pkg/balancer"
	"github.com/cloudreve/Cloudreve/v3/pkg/cluster"
	"github.com/cloudreve/Cloudreve/v3/pkg/mocks/controllermock"
	"github.com/gin-gonic/gin"
	"github.com/jinzhu/gorm"
	"github.com/stretchr/testify/assert"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestMasterMetadata(t *testing.T) {
	a := assert.New(t)
	masterMetaDataFunc := MasterMetadata()
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest("GET", "/", nil)

	c.Request.Header = map[string][]string{
		"X-Cr-Site-Id":           {"expectedSiteID"},
		"X-Cr-Site-Url":          {"expectedSiteURL"},
		"X-Cr-Cloudreve-Version": {"expectedMasterVersion"},
	}
	masterMetaDataFunc(c)
	siteID, _ := c.Get("MasterSiteID")
	siteURL, _ := c.Get("MasterSiteURL")
	siteVersion, _ := c.Get("MasterVersion")

	a.Equal("expectedSiteID", siteID.(string))
	a.Equal("expectedSiteURL", siteURL.(string))
	a.Equal("expectedMasterVersion", siteVersion.(string))
}

// fakePool 直接返回构造好的节点，不调用 Init，避免真实从机节点启动心跳协程去碰其他测试的 sqlmock
type fakePool map[uint]cluster.Node

func (p fakePool) BalanceNodeByFeature(string, balancer.Balancer) (error, cluster.Node) { return nil, nil }
func (p fakePool) GetNodeByID(id uint) cluster.Node                                      { return p[id] }
func (p fakePool) Add(*model.Node)                                                       {}
func (p fakePool) Delete(uint)                                                           {}

func TestSlaveRPCSignRequired(t *testing.T) {
	a := assert.New(t)
	key := model.NewNodeKey()
	slaveRPCSignRequiredFunc := SlaveRPCSignRequired(fakePool{
		38: &cluster.SlaveNode{Model: &model.Node{Model: gorm.Model{ID: 38}, Type: model.SlaveNodeType}},
		39: &cluster.MasterNode{Model: &model.Node{Model: gorm.Model{ID: 39}, Type: model.MasterNodeType, MasterKey: key}},
		40: &cluster.SlaveNode{Model: &model.Node{Model: gorm.Model{ID: 40}, Type: model.SlaveNodeType, MasterKey: key}},
	})
	call := func(nodeID string, instance auth.Auth) int {
		rec := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(rec)
		c.Request = httptest.NewRequest("PUT", "/api/v3/slave/upload", strings.NewReader(`{"session":{}}`))
		c.Request.Header.Set("X-Cr-Node-Id", nodeID)
		c.Request = auth.SignRequest(instance, c.Request, 60)
		slaveRPCSignRequiredFunc(c)
		if !c.IsAborted() {
			return 200
		}
		return rec.Code
	}
	empty := auth.HMACAuth{SecretKey: []byte("")}

	// 节点 ID 无法解析、节点不存在
	a.Equal(403, call("unknown", empty))
	a.Equal(403, call("41", empty))
	// 漏洞复现：节点密钥为空时，空密钥签名不能通过
	a.Equal(403, call("38", empty))
	// 主节点不接受从机回调，即使签名正确
	a.Equal(403, call("39", auth.HMACAuth{SecretKey: []byte(key)}))
	// 从机节点：空密钥、错误密钥都不行
	a.Equal(403, call("40", empty))
	a.Equal(403, call("40", auth.HMACAuth{SecretKey: []byte(model.NewNodeKey())}))

	// success
	a.Equal(200, call("40", auth.HMACAuth{SecretKey: []byte(key)}))

	// GET 请求用 URL 签名
	{
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		signed, _ := auth.SignURI(auth.HMACAuth{SecretKey: []byte(key)}, "/api/v3/slave/credential/1", 60)
		c.Request = httptest.NewRequest("GET", signed.String(), nil)
		c.Request.Header.Set("X-Cr-Node-Id", "40")
		slaveRPCSignRequiredFunc(c)
		a.False(c.IsAborted())
	}
}

func TestUseSlaveAria2Instance(t *testing.T) {
	a := assert.New(t)

	// MasterSiteID not set
	{
		testController := &controllermock.SlaveControllerMock{}
		useSlaveAria2InstanceFunc := UseSlaveAria2Instance(testController)
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest("GET", "/", nil)
		useSlaveAria2InstanceFunc(c)
		a.True(c.IsAborted())
	}

	// Cannot get aria2 instances
	{
		testController := &controllermock.SlaveControllerMock{}
		useSlaveAria2InstanceFunc := UseSlaveAria2Instance(testController)
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest("GET", "/", nil)
		c.Set("MasterSiteID", "expectedSiteID")
		testController.On("GetAria2Instance", "expectedSiteID").Return(&common.DummyAria2{}, errors.New("error"))
		useSlaveAria2InstanceFunc(c)
		a.True(c.IsAborted())
		testController.AssertExpectations(t)
	}

	// Success
	{
		testController := &controllermock.SlaveControllerMock{}
		useSlaveAria2InstanceFunc := UseSlaveAria2Instance(testController)
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest("GET", "/", nil)
		c.Set("MasterSiteID", "expectedSiteID")
		testController.On("GetAria2Instance", "expectedSiteID").Return(&common.DummyAria2{}, nil)
		useSlaveAria2InstanceFunc(c)
		a.False(c.IsAborted())
		res, _ := c.Get("MasterAria2Instance")
		a.NotNil(res)
		testController.AssertExpectations(t)
	}
}
